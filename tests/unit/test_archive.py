"""Tests for MS4 archive creation, verification, credentials, and checksums."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vaultkeep.archive import (
    ArchiveBuildRequest,
    ArchiveTools,
    PasswordSecret,
    build_archive,
    calculate_archive_sha256,
    estimate_inner_tar_size,
    private_inner_tar_path,
    verify_checksum_sidecar,
    write_checksum_sidecar,
)
from vaultkeep.archive import builder as builder_module
from vaultkeep.archive import passwords as passwords_module
from vaultkeep.archive import tar_7z as tar_7z_module
from vaultkeep.archive import tar_zstd as tar_zstd_module
from vaultkeep.archive import verification as verification_module
from vaultkeep.archive.process import CommandResult
from vaultkeep.archive.tar_input import (
    compare_tar_members,
    parse_gnu_tar_listing,
    tar_member_input,
    validate_tar_members,
)
from vaultkeep.errors import (
    ArchiveCreationError,
    ArchiveVerificationError,
    PasswordFileError,
    PlaintextCleanupError,
)
from vaultkeep.sources import SourceEntry, SourceEntryType, SourceSnapshot
from vaultkeep.state import BackupStateRecord

SOURCE_DIGEST = "sha256:" + "a" * 64
IDENTITY_HASH = "b" * 16
BACKUP_ID = "c" * 32


def _entry(
    tmp_path: Path,
    archive_path: str,
    *,
    entry_type: SourceEntryType = SourceEntryType.FILE,
    size: int = 7,
    link_target: str | None = None,
    followed_symlink: bool = False,
) -> SourceEntry:
    path = tmp_path / archive_path.replace("/", "-")
    return SourceEntry(
        absolute_path=path,
        archive_path=archive_path,
        entry_type=entry_type,
        mode=0o640,
        uid=0,
        gid=0,
        device=1,
        inode=len(archive_path),
        size=size,
        mtime_ns=1,
        ctime_ns=1,
        link_target=link_target,
        followed_symlink=followed_symlink,
        source_index=0,
    )


def _snapshot(tmp_path: Path) -> SourceSnapshot:
    return SourceSnapshot(
        (
            _entry(
                tmp_path,
                "srv/app",
                entry_type=SourceEntryType.DIRECTORY,
                size=0,
            ),
            _entry(tmp_path, "srv/app/file"),
        )
    )


def _request(
    tmp_path: Path,
    *,
    archive_format: str = "tar.zst",
    compression_level: int = 6,
) -> ArchiveBuildRequest:
    suffix = ".tar.zst" if archive_format == "tar.zst" else ".tar.7z"
    archive = tmp_path / f"backup{suffix}"
    return ArchiveBuildRequest(
        snapshot=_snapshot(tmp_path),
        expected_source_digest=SOURCE_DIGEST,
        archive_format=archive_format,  # type: ignore[arg-type]
        compression_level=compression_level,
        archive_path=archive,
        checksum_path=Path(str(archive) + ".sha256"),
        job_id="app",
        job_identity_hash=IDENTITY_HASH,
        backup_id=BACKUP_ID,
        local_temp_root=tmp_path / "private",
        tools=ArchiveTools(
            tar=Path("/tools/tar"),
            zstd=Path("/tools/zstd"),
            seven_zip=Path("/tools/7z"),
        ),
    )


def test_tar_member_input_is_complete_raw_and_nul_delimited(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)

    assert tar_member_input(snapshot) == b"srv/app\0srv/app/file\0"


def test_gnu_tar_escape_listing_round_trip_and_expected_members(tmp_path: Path) -> None:
    snapshot = SourceSnapshot(
        (
            _entry(tmp_path, "srv/back\\slash"),
            _entry(tmp_path, "srv/line\nbreak"),
            _entry(tmp_path, "srv/raw-\xff"),
        )
    )
    listing = b"srv/back\\\\slash\nsrv/line\\nbreak\nsrv/raw-\\303\\277\n"

    parsed = parse_gnu_tar_listing(listing)

    assert parsed == (
        b"srv/back\\slash",
        b"srv/line\nbreak",
        os.fsencode("srv/raw-\xff"),
    )
    compare_tar_members(snapshot, parsed)


@pytest.mark.parametrize(
    "members",
    [
        (b"/absolute",),
        (b"safe/../escape",),
        (b"duplicate", b"duplicate"),
        (b"",),
    ],
)
def test_tar_member_validation_rejects_unsafe_structures(
    members: tuple[bytes, ...],
) -> None:
    with pytest.raises(ArchiveVerificationError):
        validate_tar_members(members)


def test_tar_member_comparison_accepts_directory_listing_slash(tmp_path: Path) -> None:
    snapshot = SourceSnapshot(
        (_entry(tmp_path, "srv/empty", entry_type=SourceEntryType.DIRECTORY, size=0),)
    )

    compare_tar_members(snapshot, (b"srv/empty/",))


def test_checksum_sidecar_round_trip_and_exact_format(tmp_path: Path) -> None:
    archive = tmp_path / "backup.tar.zst"
    sidecar = tmp_path / "backup.tar.zst.sha256"
    archive.write_bytes(b"archive-content")
    digest = calculate_archive_sha256(archive)

    write_checksum_sidecar(archive, sidecar, digest)

    assert sidecar.read_bytes() == f"{digest}  {archive.name}\n".encode()
    assert verify_checksum_sidecar(archive, sidecar) == digest


def test_checksum_verification_rejects_changed_archive(tmp_path: Path) -> None:
    archive = tmp_path / "backup.tar.zst"
    sidecar = tmp_path / "backup.tar.zst.sha256"
    archive.write_bytes(b"before")
    write_checksum_sidecar(archive, sidecar, calculate_archive_sha256(archive))
    archive.write_bytes(b"after")

    with pytest.raises(ArchiveVerificationError, match="does not match"):
        verify_checksum_sidecar(archive, sidecar)


def test_checksum_verification_rejects_malformed_sidecar(tmp_path: Path) -> None:
    archive = tmp_path / "backup.tar.zst"
    sidecar = tmp_path / "backup.tar.zst.sha256"
    archive.write_bytes(b"archive")
    sidecar.write_text("not-a-checksum\n", encoding="utf-8")

    with pytest.raises(ArchiveVerificationError, match="format"):
        verify_checksum_sidecar(archive, sidecar)


def test_checksum_write_never_overwrites_existing_sidecar(tmp_path: Path) -> None:
    archive = tmp_path / "backup.tar.zst"
    sidecar = tmp_path / "backup.tar.zst.sha256"
    archive.write_bytes(b"archive")
    sidecar.write_text("operator data", encoding="utf-8")

    with pytest.raises(ArchiveCreationError):
        write_checksum_sidecar(archive, sidecar, calculate_archive_sha256(archive))

    assert sidecar.read_text(encoding="utf-8") == "operator data"


def test_password_secret_is_redacted_and_cleared() -> None:
    secret = PasswordSecret(b"do-not-log")

    assert repr(secret) == "PasswordSecret(<redacted>)"
    assert secret.pipe_input() == b"do-not-log\n"
    secret.clear()

    with pytest.raises(PasswordFileError, match="cleared"):
        secret.pipe_input()


@pytest.mark.parametrize("value", [b"", b"bad\n", b"bad\r", b"bad\0", b"\xff"])
def test_password_secret_rejects_invalid_direct_values(value: bytes) -> None:
    with pytest.raises(PasswordFileError):
        PasswordSecret(value)


@pytest.mark.parametrize(
    "raw",
    [b"", b"\n", b"line\nother", b"carriage\r", b"null\0", b"\xff"],
)
def test_invalid_passphrases_are_rejected(raw: bytes) -> None:
    with pytest.raises(PasswordFileError):
        passwords_module._validate_passphrase(raw)


def test_passphrase_removes_exactly_one_trailing_lf() -> None:
    assert passwords_module._validate_passphrase(b"  secret  \n") == b"  secret  "


def test_password_file_loader_uses_no_follow_and_returns_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password_file = (tmp_path / "secret").absolute()
    password_file.write_bytes(b"secret\n")
    original_open = passwords_module.os.open
    observed_flags = 0

    def record_open(path: Any, flags: int, *args: Any) -> int:
        nonlocal observed_flags
        observed_flags = flags
        return original_open(path, flags, *args)

    monkeypatch.setattr(passwords_module.os, "name", "posix")
    monkeypatch.setattr(passwords_module, "_disable_core_dumps", lambda: None)
    monkeypatch.setattr(
        passwords_module,
        "_validate_password_path",
        lambda path: path.stat(),
    )
    monkeypatch.setattr(passwords_module.os, "open", record_open)

    loaded = passwords_module.load_password_file(password_file)

    assert loaded.secret.pipe_input() == b"secret\n"
    assert loaded.fingerprint.size == len(b"secret\n")
    if hasattr(os, "O_NOFOLLOW"):
        assert observed_flags & os.O_NOFOLLOW
    loaded.secret.clear()


def test_password_path_enforces_root_file_and_secure_parent_contract() -> None:
    class FakePath:
        def __init__(self, label: str, status: Any) -> None:
            self.label = label
            self.status = status
            self.parent = self

        def lstat(self) -> Any:
            return self.status

        def __str__(self) -> str:
            return self.label

    root = FakePath("/", SimpleNamespace(st_mode=stat.S_IFDIR | 0o755))
    secrets = FakePath("/etc/vaultkeep/secrets", SimpleNamespace(st_mode=stat.S_IFDIR | 0o700))
    password = FakePath(
        "/etc/vaultkeep/secrets/app.passphrase",
        SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_gid=0),
    )
    secrets.parent = root
    password.parent = secrets

    result = passwords_module._validate_password_path(password)  # type: ignore[arg-type]

    assert result is password.status


@pytest.mark.parametrize(
    ("file_mode", "uid", "gid", "parent_mode", "message"),
    [
        (stat.S_IFLNK | 0o600, 0, 0, 0o700, "regular file"),
        (stat.S_IFREG | 0o600, 1000, 0, 0o700, "root:root"),
        (stat.S_IFREG | 0o600, 0, 1000, 0o700, "root:root"),
        (stat.S_IFREG | 0o640, 0, 0, 0o700, "0600"),
        (stat.S_IFREG | 0o600, 0, 0, 0o770, "writable"),
    ],
)
def test_password_path_rejects_insecure_metadata(
    file_mode: int,
    uid: int,
    gid: int,
    parent_mode: int,
    message: str,
) -> None:
    class FakePath:
        def __init__(self, label: str, status: Any) -> None:
            self.label = label
            self.status = status
            self.parent = self

        def lstat(self) -> Any:
            return self.status

        def __str__(self) -> str:
            return self.label

    root = FakePath("/", SimpleNamespace(st_mode=stat.S_IFDIR | 0o755))
    parent = FakePath("/secure", SimpleNamespace(st_mode=stat.S_IFDIR | parent_mode))
    password = FakePath(
        "/secure/password",
        SimpleNamespace(st_mode=file_mode, st_uid=uid, st_gid=gid),
    )
    parent.parent = root
    password.parent = parent

    with pytest.raises(PasswordFileError, match=message):
        passwords_module._validate_password_path(password)  # type: ignore[arg-type]


def test_password_file_identity_change_is_rejected() -> None:
    fields = {
        "st_dev": 1,
        "st_ino": 2,
        "st_mode": stat.S_IFREG | 0o600,
        "st_uid": 0,
        "st_gid": 0,
        "st_size": 10,
        "st_mtime_ns": 3,
        "st_ctime_ns": 4,
    }
    expected = SimpleNamespace(**fields)
    actual = SimpleNamespace(**(fields | {"st_ino": 99}))

    with pytest.raises(PasswordFileError, match="changed"):
        passwords_module._require_same_file(
            expected,  # type: ignore[arg-type]
            actual,  # type: ignore[arg-type]
            Path("/secure/password"),
        )


def test_private_tar_path_validates_all_identity_components(tmp_path: Path) -> None:
    expected = tmp_path / f"app-{IDENTITY_HASH}" / BACKUP_ID / "app.tar"

    assert (
        private_inner_tar_path(
            temp_root=tmp_path,
            job_id="app",
            job_identity_hash=IDENTITY_HASH,
            backup_id=BACKUP_ID,
        )
        == expected
    )
    with pytest.raises(ValueError):
        private_inner_tar_path(
            temp_root=tmp_path,
            job_id="../app",
            job_identity_hash=IDENTITY_HASH,
            backup_id=BACKUP_ID,
        )


def test_inner_tar_estimate_uses_full_logical_file_size(tmp_path: Path) -> None:
    small = SourceSnapshot((_entry(tmp_path, "file", size=1),))
    sparse = SourceSnapshot((_entry(tmp_path, "file", size=10_000_000),))

    assert estimate_inner_tar_size(sparse) > estimate_inner_tar_size(small) + 9_000_000


def test_private_capacity_requires_estimate_and_safety_margin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = SourceSnapshot((_entry(tmp_path, "file", size=1_000_000),))

    monkeypatch.setattr(
        tar_7z_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=1),
    )
    with pytest.raises(ArchiveCreationError, match="Insufficient"):
        tar_7z_module.validate_private_capacity(snapshot, tmp_path)

    monkeypatch.setattr(
        tar_7z_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=10**9),
    )
    assert tar_7z_module.validate_private_capacity(snapshot, tmp_path) > 64 * 1024 * 1024


def test_private_plaintext_cleanup_removes_file_and_backup_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "backup"
    workspace.mkdir()
    inner = workspace / "app.tar"
    inner.write_bytes(b"plaintext")

    tar_7z_module.cleanup_private_inner_tar(inner)

    assert not workspace.exists()


def test_tar_zstd_creation_uses_required_gnu_tar_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(tmp_path)
    output = tmp_path / "backup.tar.zst"
    observed: dict[str, Any] = {}

    def fake_pipeline(
        producer: tuple[Any, ...],
        consumer: tuple[Any, ...],
        **options: Any,
    ) -> None:
        observed.update(producer=producer, consumer=consumer, options=options)
        output.write_bytes(b"compressed")

    monkeypatch.setattr(tar_zstd_module, "run_pipeline", fake_pipeline)

    tar_zstd_module.create_tar_zstd(
        snapshot,
        output,
        compression_level=9,
        tools=ArchiveTools(tar=Path("/tar"), zstd=Path("/zstd")),
    )

    producer = observed["producer"]
    assert "--format=gnu" in producer
    assert "--null" in producer
    assert "--verbatim-files-from" in producer
    assert "--no-recursion" in producer
    assert observed["options"]["producer_input"] == tar_member_input(snapshot)
    assert "-9" in observed["consumer"]


def test_tar_zstd_failure_removes_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "backup.tar.zst"

    def fail_pipeline(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        output.write_bytes(b"partial")
        raise ArchiveCreationError("forced failure")

    monkeypatch.setattr(tar_zstd_module, "run_pipeline", fail_pipeline)

    with pytest.raises(ArchiveCreationError):
        tar_zstd_module.create_tar_zstd(
            _snapshot(tmp_path),
            output,
            compression_level=6,
            tools=ArchiveTools(tar=Path("/tar"), zstd=Path("/zstd")),
        )

    assert not output.exists()


def test_tar_zstd_reports_partial_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "backup.tar.zst"
    monkeypatch.setattr(
        tar_zstd_module,
        "run_pipeline",
        lambda *args, **kwargs: (_ for _ in ()).throw(ArchiveCreationError("creation failed")),
    )
    monkeypatch.setattr(
        tar_zstd_module,
        "_remove_partial",
        lambda path: (_ for _ in ()).throw(ArchiveCreationError(f"cannot remove partial {path}")),
    )

    with pytest.raises(ArchiveCreationError, match="cannot remove partial"):
        tar_zstd_module.create_tar_zstd(
            _snapshot(tmp_path),
            output,
            compression_level=6,
            tools=ArchiveTools(tar=Path("/tar"), zstd=Path("/zstd")),
        )


def test_inner_tar_creation_uses_exclusive_mode_0600(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "app.tar"
    observed: dict[str, Any] = {}

    def fake_run(command: tuple[Any, ...], **options: Any) -> CommandResult:
        observed.update(command=command, options=options)
        output.write_bytes(b"gnu tar")
        return CommandResult(0, b"", b"")

    monkeypatch.setattr(tar_zstd_module, "run_command", fake_run)

    tar_zstd_module.create_inner_tar(
        _snapshot(tmp_path),
        output,
        tools=ArchiveTools(tar=Path("/tar")),
    )

    assert "--format=gnu" in observed["command"]
    assert observed["options"]["input_data"] == tar_member_input(_snapshot(tmp_path))
    if os.name == "posix":
        assert output.stat().st_mode & 0o777 == 0o600


def test_7z_encryption_passes_password_only_through_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = tmp_path / "app.tar"
    output = tmp_path / "backup.tar.7z"
    inner.write_bytes(b"tar")
    observed: dict[str, Any] = {}

    def fake_run(arguments: tuple[Any, ...], **options: Any) -> CommandResult:
        observed.update(arguments=arguments, options=options)
        output.write_bytes(b"encrypted")
        return CommandResult(0, b"", b"")

    monkeypatch.setattr(tar_7z_module, "run_command", fake_run)
    secret = PasswordSecret(b"private-value")

    tar_7z_module.encrypt_inner_tar(
        inner,
        output,
        compression_level=6,
        password=secret,
        tools=ArchiveTools(seven_zip=Path("/usr/bin/7z")),
    )

    rendered_arguments = "\0".join(os.fspath(value) for value in observed["arguments"])
    assert "private-value" not in rendered_arguments
    assert "-p" in observed["arguments"]
    assert observed["options"]["terminal_input"] == b"private-value\nprivate-value\n"


def test_tar_zstd_verification_checks_stream_and_exact_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    archive = tmp_path / "backup.tar.zst"
    archive.write_bytes(b"archive")

    def fake_run(*args: Any, **kwargs: Any) -> CommandResult:
        del args, kwargs
        calls.append("test")
        return CommandResult(0, b"", b"")

    def fake_pipeline(*args: Any, **kwargs: Any) -> None:
        del args
        calls.append("list")
        kwargs["consumer_stdout"].write(b"srv/app\nsrv/app/file\n")

    monkeypatch.setattr(verification_module, "run_command", fake_run)
    monkeypatch.setattr(verification_module, "run_pipeline", fake_pipeline)

    verification_module.verify_tar_zstd(
        archive,
        _snapshot(tmp_path),
        tools=ArchiveTools(tar=Path("/tar"), zstd=Path("/zstd")),
    )

    assert calls == ["test", "list"]


def test_7z_verification_requires_hidden_header_and_one_inner_tar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "backup.tar.7z"
    archive.write_bytes(b"archive")
    responses = iter(
        (
            CommandResult(0, b"", b""),
            CommandResult(2, b"", b"password required"),
            CommandResult(0, b"Path = app.tar\n", b""),
        )
    )

    def fake_run(*args: Any, **kwargs: Any) -> CommandResult:
        del args, kwargs
        return next(responses)

    def fake_pipeline(*args: Any, **kwargs: Any) -> None:
        del args
        kwargs["consumer_stdout"].write(b"srv/app\nsrv/app/file\n")

    monkeypatch.setattr(verification_module, "run_command", fake_run)
    monkeypatch.setattr(verification_module, "run_pipeline", fake_pipeline)

    verification_module.verify_tar_7z(
        archive,
        _snapshot(tmp_path),
        job_id="app",
        password=PasswordSecret(b"secret"),
        tools=ArchiveTools(tar=Path("/tar"), seven_zip=Path("/7z")),
    )


def test_7z_verification_rejects_exposed_inner_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "backup.tar.7z"
    archive.write_bytes(b"archive")
    responses = iter(
        (
            CommandResult(0, b"", b""),
            CommandResult(0, b"Path = app.tar\n", b""),
        )
    )
    monkeypatch.setattr(
        verification_module,
        "run_command",
        lambda *args, **kwargs: next(responses),
    )

    with pytest.raises(ArchiveVerificationError, match="conceal"):
        verification_module.verify_tar_7z(
            archive,
            _snapshot(tmp_path),
            job_id="app",
            password=PasswordSecret(b"secret"),
            tools=ArchiveTools(tar=Path("/tar"), seven_zip=Path("/7z")),
        )


def test_7z_password_test_and_state_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "backup.tar.7z"
    secret = PasswordSecret(b"secret")
    observed: list[Path] = []

    def fake_test(
        path: Path,
        password: PasswordSecret,
        *,
        tools: ArchiveTools,
    ) -> bool:
        del password, tools
        observed.append(path)
        return True

    monkeypatch.setattr(verification_module, "test_7z_password", fake_test)
    verifier = verification_module.encrypted_backup_credential_verifier(
        secret,
        tools=ArchiveTools(seven_zip=Path("/7z")),
    )
    record = BackupStateRecord(
        job_id="app",
        backup_id=BACKUP_ID,
        created_at_utc="2026-07-23T09:00:00Z",
        source_digest=SOURCE_DIGEST,
        config_fingerprint="sha256:" + "d" * 64,
        backup_path=str(archive),
        application_version="0.1.0.dev0",
        encrypted=True,
    )

    assert verifier(record) is True
    assert observed == [archive]
    assert (
        verifier(
            BackupStateRecord(
                job_id=record.job_id,
                backup_id=record.backup_id,
                created_at_utc=record.created_at_utc,
                source_digest=record.source_digest,
                config_fingerprint=record.config_fingerprint,
                backup_path=record.backup_path,
                application_version=record.application_version,
                encrypted=False,
            )
        )
        is False
    )


def test_build_archive_completes_checksum_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)

    def fake_create(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        request.archive_path.write_bytes(b"verified archive")

    monkeypatch.setattr(builder_module, "create_tar_zstd", fake_create)
    monkeypatch.setattr(builder_module, "verify_tar_zstd", lambda *args, **kwargs: None)

    artifact = build_archive(
        request,
        source_digest_calculator=lambda snapshot: SOURCE_DIGEST,
    )

    assert artifact.sha256 == calculate_archive_sha256(request.archive_path)
    assert artifact.size == len(b"verified archive")
    assert verify_checksum_sidecar(request.archive_path, request.checksum_path) == artifact.sha256


def test_build_archive_source_change_removes_partial_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)

    def fake_create(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        request.archive_path.write_bytes(b"partial")

    monkeypatch.setattr(builder_module, "create_tar_zstd", fake_create)

    with pytest.raises(ArchiveVerificationError, match="changed"):
        build_archive(
            request,
            source_digest_calculator=lambda snapshot: "sha256:" + "f" * 64,
        )

    assert not request.archive_path.exists()
    assert not request.checksum_path.exists()


def test_build_archive_preserves_preexisting_checksum_on_exclusive_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    request.checksum_path.write_text("operator data", encoding="utf-8")

    def fake_create(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        request.archive_path.write_bytes(b"archive")

    monkeypatch.setattr(builder_module, "create_tar_zstd", fake_create)
    monkeypatch.setattr(builder_module, "verify_tar_zstd", lambda *args, **kwargs: None)

    with pytest.raises(ArchiveCreationError):
        build_archive(
            request,
            source_digest_calculator=lambda snapshot: SOURCE_DIGEST,
        )

    assert request.checksum_path.read_text(encoding="utf-8") == "operator data"
    assert not request.archive_path.exists()


def test_encrypted_build_cleans_plaintext_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path, archive_format="tar.7z")
    inner = tmp_path / "private" / "job" / "backup" / "app.tar"
    events: list[str] = []

    monkeypatch.setattr(builder_module, "private_inner_tar_path", lambda **kwargs: inner)

    def prepare(path: Path) -> None:
        path.parent.mkdir(parents=True)
        events.append("prepare")

    def create(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        inner.write_bytes(b"plaintext")
        events.append("create")

    def encrypt(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        request.archive_path.write_bytes(b"encrypted")
        events.append("encrypt")

    def cleanup(path: Path) -> None:
        path.unlink()
        path.parent.rmdir()
        events.append("cleanup")

    monkeypatch.setattr(builder_module, "prepare_private_workspace", prepare)
    monkeypatch.setattr(builder_module, "create_private_inner_tar", create)
    monkeypatch.setattr(builder_module, "encrypt_inner_tar", encrypt)
    monkeypatch.setattr(builder_module, "verify_tar_7z", lambda *args, **kwargs: None)
    monkeypatch.setattr(builder_module, "cleanup_private_inner_tar", cleanup)

    artifact = build_archive(
        request,
        password=PasswordSecret(b"secret"),
        source_digest_calculator=lambda snapshot: SOURCE_DIGEST,
    )

    assert artifact.archive_format == "tar.7z"
    assert not inner.exists()
    assert events == ["prepare", "create", "encrypt", "cleanup"]


def test_plaintext_cleanup_failure_blocks_encrypted_build_and_removes_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path, archive_format="tar.7z")
    inner = tmp_path / "private" / "app.tar"
    monkeypatch.setattr(builder_module, "private_inner_tar_path", lambda **kwargs: inner)
    monkeypatch.setattr(
        builder_module,
        "prepare_private_workspace",
        lambda path: path.parent.mkdir(parents=True),
    )
    monkeypatch.setattr(
        builder_module,
        "create_private_inner_tar",
        lambda *args, **kwargs: inner.write_bytes(b"plaintext"),
    )
    monkeypatch.setattr(
        builder_module,
        "encrypt_inner_tar",
        lambda *args, **kwargs: request.archive_path.write_bytes(b"encrypted"),
    )
    monkeypatch.setattr(builder_module, "verify_tar_7z", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        builder_module,
        "cleanup_private_inner_tar",
        lambda path: (_ for _ in ()).throw(PlaintextCleanupError(f"cannot remove {path}")),
    )

    with pytest.raises(PlaintextCleanupError, match="cannot remove"):
        build_archive(
            request,
            password=PasswordSecret(b"secret"),
            source_digest_calculator=lambda snapshot: SOURCE_DIGEST,
        )

    assert not request.archive_path.exists()


@pytest.mark.parametrize(
    ("archive_format", "level", "password"),
    [
        ("tar.zst", 20, None),
        ("tar.zst", 6, PasswordSecret(b"unexpected")),
        ("tar.7z", 10, PasswordSecret(b"secret")),
        ("tar.7z", 6, None),
    ],
)
def test_build_request_rejects_invalid_format_contract(
    tmp_path: Path,
    archive_format: str,
    level: int,
    password: PasswordSecret | None,
) -> None:
    with pytest.raises(ValueError):
        build_archive(
            _request(
                tmp_path,
                archive_format=archive_format,
                compression_level=level,
            ),
            password=password,
            source_digest_calculator=lambda snapshot: SOURCE_DIGEST,
        )
