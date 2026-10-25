"""Manual verification of real archive streams independently of source files."""

from __future__ import annotations

import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from vaultkeep.archive import ArchiveTools, PasswordSecret
from vaultkeep.archive import passwords as passwords_module
from vaultkeep.archive.checksums import calculate_archive_sha256, write_checksum_sidecar
from vaultkeep.archive.process import run_command
from vaultkeep.archive.tar_7z import encrypt_inner_tar
from vaultkeep.backup.workflow import load_validated_config
from vaultkeep.cli.commands import main
from vaultkeep.destination import allocate_job_backup_paths, create_staging_directory
from vaultkeep.destination.manifest import BackupManifest, write_manifest
from vaultkeep.file_backups import file_config

TOOLS = ArchiveTools()
pytestmark = pytest.mark.skipif(
    not all(tool.is_file() for tool in (TOOLS.tar, TOOLS.zstd, TOOLS.seven_zip)),
    reason="Real archive tools are unavailable",
)


@pytest.fixture(params=["tar.zst", "tar.7z"])
def job(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    valid_config["sources"][0]["path"] = str(tmp_path / "missing-source")
    valid_config["destination"]["root"] = str(tmp_path / "backups")
    (tmp_path / "backups").mkdir()
    valid_config["archive"]["format"] = request.param
    if request.param == "tar.7z":
        password = tmp_path / "password"
        password.write_bytes(b"secret\n")
        password.chmod(0o600)
        valid_config["encryption"] = {"mode": "password", "password_file": str(password)}
        # /tmp has a writable ancestor; ownership/path policy has separate unit tests.
        # Keep real file loading and real password delivery to 7-Zip.
        monkeypatch.setattr(
            passwords_module, "_validate_password_path", lambda path, **kwargs: path.lstat()
        )
    path = tmp_path / "app.yaml"
    path.write_text(json.dumps(valid_config))
    return path


def _tar(*members: str) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for member in members:
            info = tarfile.TarInfo(member)
            info.size = 4
            archive.addfile(info, io.BytesIO(b"data"))
    return output.getvalue()


def _backup(
    job: Path,
    payload: bytes,
    *,
    number: int = 1,
    inner_name: str = "app.tar",
    encrypt_headers: bool = True,
) -> Path:
    config = load_validated_config(job)
    if config.backup_mode == "per_file":
        config = file_config(config, Path(config.sources[0].path) / f"wallet-{number}")
        Path(config.destination.root).mkdir()
    created = datetime(2026, 10, 24, number, tzinfo=UTC)
    paths = allocate_job_backup_paths(
        config.destination,
        job_id=config.job.id,
        backup_id=f"{number:032x}",
        hostname="host",
        created_at=created,
        source_digest="sha256:" + "a" * 64,
        archive_format=config.archive.format,
    )
    create_staging_directory(paths)
    with TemporaryDirectory(dir=job.parent) as workspace:
        inner = Path(workspace) / inner_name
        inner.write_bytes(payload)
        if config.archive.format == "tar.zst":
            run_command((TOOLS.zstd, "--quiet", "-o", paths.archive_path, inner))
        else:
            with PasswordSecret(b"secret") as password:
                if encrypt_headers:
                    encrypt_inner_tar(
                        inner,
                        paths.archive_path,
                        compression_level=1,
                        password=password,
                        tools=TOOLS,
                    )
                else:
                    run_command(
                        (
                            TOOLS.seven_zip,
                            "a",
                            "-t7z",
                            "-mhe=off",
                            "-p",
                            "--",
                            paths.archive_path,
                            inner.name,
                        ),
                        cwd=inner.parent,
                        terminal_input=password.terminal_input(confirm=True),
                    )
    digest = calculate_archive_sha256(paths.archive_path)
    write_checksum_sidecar(paths.archive_path, paths.checksum_path, digest)
    write_manifest(
        paths.manifest_path,
        BackupManifest(
            application_version="0.3.0.dev1",
            backup_id=f"{number:032x}",
            job=config.job.id,
            created_at=created.isoformat(),
            created_at_utc=created.isoformat().replace("+00:00", "Z"),
            source_digest="sha256:" + "a" * 64,
            config_fingerprint="sha256:" + "b" * 64,
            archive=paths.archive_path.name,
            archive_digest=f"sha256:{digest}",
            archive_format=config.archive.format,
            encrypted=config.archive.format == "tar.7z",
            hostname="host",
        ),
    )
    paths.staging_directory.rename(paths.final_directory)
    return paths.final_directory / paths.archive_path.name


def _files(root: Path) -> dict[Path, tuple[bytes, int]]:
    return {
        path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("backup_mode", ["combined", "per_file"])
def test_verify_real_archives_without_sources_preserves_backups_and_state(
    job: Path, backup_mode: str, capsys: pytest.CaptureFixture[str]
) -> None:
    data = json.loads(job.read_text())
    data["backup_mode"] = backup_mode
    job.write_text(json.dumps(data))
    archives = [_backup(job, _tar("app/data"), number=n) for n in (1, 2)]
    (job.parent / "local-state.json").write_bytes(b"existing local state")
    before = _files(job.parent)

    assert main(["--user", "--config", str(job), "verify"]) == 0

    output = capsys.readouterr()
    assert output.out == "verify: verified\n"
    assert "Backups:  2/2 validated\nArchives: 2/2 verified\nResult:   VERIFIED" in output.err
    operation = (
        "Zstandard stream test"
        if load_validated_config(job).archive.format == "tar.zst"
        else "7-Zip archive test"
    )
    assert output.err.index("SHA-256 verification complete") < output.err.index(operation)
    for archive in archives:
        assert f"Archive verification: OK | {archive}" in output.err
    assert _files(job.parent) == before


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"not a TAR archive", "verification failed"),
        (_tar("/absolute"), "absolute member path"),
        (_tar("app/../escape"), "parent-traversal member path"),
        (_tar("app/data", "app/data"), "duplicate member paths"),
    ],
)
def test_verify_rejects_invalid_tar_with_matching_checksums(
    job: Path, payload: bytes, reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = _backup(job, payload)
    good = _backup(job, _tar("app/good"), number=2)
    before = _files(job.parent)

    assert main(["--user", "--config", str(job), "verify"]) == 7

    output = capsys.readouterr()
    assert output.out == ""
    assert str(bad) in output.err
    assert reason in output.err
    assert f"Archive verification: OK | {good}" in output.err
    assert "Backups:  2/2 validated\nArchives: 1/2 verified\nResult:   FAILED" in output.err
    assert _files(job.parent) == before


def test_verify_rejects_invalid_compressed_bytes_with_matching_checksums(
    job: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = _backup(job, _tar("app/data"))
    archive.write_bytes(b"invalid compressed bytes")
    digest = calculate_archive_sha256(archive)
    sidecar = archive.with_suffix(archive.suffix + ".sha256")
    sidecar.unlink()
    write_checksum_sidecar(archive, sidecar, digest)
    manifest_path = next(archive.parent.glob("*.json"))
    manifest = json.loads(manifest_path.read_text())
    manifest["archive_digest"] = f"sha256:{digest}"
    manifest_path.write_text(json.dumps(manifest))

    assert main(["--user", "--config", str(job), "verify"]) == 7
    output = capsys.readouterr()
    assert "malformed backup" not in output.err
    assert str(archive) in output.err
    assert "verification failed" in output.err


def test_verify_rejects_wrong_password(job: Path, capsys: pytest.CaptureFixture[str]) -> None:
    if load_validated_config(job).archive.format != "tar.7z":
        pytest.skip("Password verification applies to encrypted archives")
    archives = [_backup(job, _tar("app/data"), number=n) for n in (1, 2)]
    (job.parent / "password").write_bytes(b"wrong-secret\n")
    before = _files(job.parent)

    assert main(["--user", "--config", str(job), "verify"]) == 7
    output = capsys.readouterr()
    for archive in archives:
        assert str(archive) in output.err
    assert "Archive verification: OK" not in output.err
    assert "wrong-secret" not in output.err
    assert _files(job.parent) == before


@pytest.mark.parametrize(
    ("inner_name", "encrypt_headers", "reason"),
    [("other.tar", True, "exactly one member"), ("app.tar", False, "conceal")],
)
def test_verify_requires_one_named_tar_and_encrypted_headers(
    job: Path,
    inner_name: str,
    encrypt_headers: bool,
    reason: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if load_validated_config(job).archive.format != "tar.7z":
        pytest.skip("Outer member verification applies to encrypted archives")
    archive = _backup(job, _tar("app/data"), inner_name=inner_name, encrypt_headers=encrypt_headers)

    assert main(["--user", "--config", str(job), "verify"]) == 7
    output = capsys.readouterr()
    assert str(archive) in output.err
    assert reason in output.err
