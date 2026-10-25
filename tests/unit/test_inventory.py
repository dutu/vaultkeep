"""Read-only inventory facts and presentation shared with verification."""

from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaultkeep.backup import workflow
from vaultkeep.cli import commands
from vaultkeep.cli import output as output_module
from vaultkeep.cli.inventory import print_inventory
from vaultkeep.cli.output import Console, format_table
from vaultkeep.destination.manifest import BackupManifest, write_manifest
from vaultkeep.destination.templates import allocate_job_backup_paths


@pytest.fixture
def job(tmp_path, valid_config):
    root = tmp_path / "backups"
    root.mkdir()
    valid_config["destination"]["root"] = str(root)
    valid_config["destination"]["name_template"] = "{job}-{format}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
    valid_config["sources"][0]["path"] = str(tmp_path / "absent-source")
    config_path = tmp_path / "app.yaml"
    config_path.write_text(json.dumps(valid_config))

    def create(number, *, created=None, archive_format="tar.zst", job_id="app", destination=None):
        config = workflow.load_validated_config(config_path)
        if destination is not None:
            config = config.model_copy(
                update={
                    "destination": config.destination.model_copy(update={"root": str(destination)})
                }
            )
        created = created or datetime(2026, 10, 23, number, tzinfo=UTC)
        paths = allocate_job_backup_paths(
            config.destination,
            job_id=job_id,
            backup_id=f"{number:032x}",
            hostname="host",
            created_at=created,
            source_digest="sha256:" + "a" * 64,
            archive_format=archive_format,
        )
        directory = paths.final_directory
        directory.mkdir(parents=True)
        archive = directory / paths.archive_path.name
        archive.write_bytes(b"x" * (number * 1024))
        (directory / paths.checksum_path.name).write_text("checksum contents are not inspected")
        manifest = BackupManifest(
            application_version="0.3.0.dev1",
            backup_id=f"{number:032x}",
            job=job_id,
            created_at=created.isoformat(),
            created_at_utc=created.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            source_digest="sha256:" + "a" * 64,
            config_fingerprint="sha256:" + "b" * 64,
            archive=archive.name,
            archive_digest="sha256:" + "c" * 64,
            archive_format=archive_format,
            encrypted=archive_format == "tar.7z",
            hostname="host",
        )
        manifest_path = directory / paths.manifest_path.name
        write_manifest(manifest_path, manifest)
        return SimpleNamespace(directory=directory, archive=archive, manifest=manifest_path)

    return SimpleNamespace(path=config_path, root=root, create=create, data=valid_config)


def cli(job, *options):
    return commands.main(["--user", "--config", str(job.path), "list", *options])


def snapshot(root):
    return {
        path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_inventory_uses_stored_utc_encryption_sizes_and_relative_paths(job, capsys):
    first = job.create(1)
    newest = job.create(
        3, created=datetime(2026, 10, 24, 8, 5, 18, tzinfo=timezone(timedelta(hours=3)))
    )
    encrypted = job.create(2, archive_format="tar.7z")
    unrelated = job.create(4, job_id="other-job")
    newest.archive.write_bytes(b"x" * 1536)

    inventory = workflow.list_backups(job.path)
    assert [backup.archive_path for backup in inventory.backups] == [
        newest.archive,
        encrypted.archive,
        first.archive,
    ]
    assert [backup.archive_size for backup in inventory.backups] == [1536, 2048, 1024]
    assert inventory.malformed == ()
    assert cli(job) == 0
    out, err = capsys.readouterr()
    assert out.startswith(f"Vaultkeep — Backups\n\nDestination: {job.root}\n\n")
    assert "Created (UTC)" in out and "Encrypted" in out
    assert "2026-10-24 05:05:18  1.5 KiB  no" in out
    assert "2026-10-23 02:00:00  2.0 KiB  yes" in out
    paths = [str(item.archive.relative_to(job.root)) for item in (newest, encrypted, first)]
    assert out.index(paths[0]) < out.index(paths[1]) < out.index(paths[2])
    assert str(newest.archive) not in out
    assert unrelated.directory.name not in out
    assert out.rstrip().endswith("Backups: 3")
    assert "VERIFIED" not in out and "Result:" not in out
    assert "list: listed" not in out and "SUCCESS" not in out
    assert "[00:" not in out and err == ""


@pytest.mark.parametrize("verbose", [False, True])
def test_inventory_never_reads_contents_or_changes_backups_config_or_state(
    job, monkeypatch, capsys, verbose
):
    plain = job.create(1)
    encrypted = job.create(2, archive_format="tar.7z")
    (job.path.parent / "local-state.json").write_text('{"untouched":true}')
    before = snapshot(job.path.parent)
    original_open = Path.open

    def guarded_open(path, mode="r", *args, **kwargs):
        assert not any(flag in mode for flag in ("w", "a", "+", "x"))
        assert path not in (plain.archive, encrypted.archive)
        assert path.suffix != ".sha256"
        return original_open(path, mode, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Inventory must not verify archives, load passwords, or probe writability")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", guarded_open)
        for name in (
            "verify_tar_zstd",
            "verify_tar_7z",
            "load_password_file",
            "probe_destination_writability",
        ):
            patch.setattr(workflow, name, forbidden)
        assert cli(job, *(["--verbose"] if verbose else [])) == 0
    assert snapshot(job.path.parent) == before
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "damage,expected",
    [
        ("manifest-missing", "Missing manifest"),
        ("invalid-json", "Invalid JSON backup manifest"),
        ("invalid-encoding", "Cannot read backup manifest"),
        ("encryption-missing", "Invalid backup manifest"),
        ("archive-missing", "Required backup artifact is missing"),
        ("checksum-missing", "Required backup artifact is missing"),
        ("archive-link", "not a regular file"),
        ("wrong-job", "Manifest identity"),
    ],
)
@pytest.mark.parametrize("with_valid", [False, True])
def test_invalid_entries_warn_and_are_excluded_without_changing_exit_code(
    job, capsys, damage, expected, with_valid
):
    bad = job.create(1)
    if damage == "manifest-missing":
        bad.manifest.unlink()
    elif damage == "invalid-json":
        bad.manifest.write_text("{invalid}")
    elif damage == "invalid-encoding":
        bad.manifest.write_bytes(b"\xff")
    elif damage in ("encryption-missing", "wrong-job"):
        metadata = json.loads(bad.manifest.read_text())
        if damage == "encryption-missing":
            metadata.pop("encrypted")
        else:
            metadata["job"] = "other-job"
        bad.manifest.write_text(json.dumps(metadata))
    elif damage == "checksum-missing":
        next(bad.directory.glob("*.sha256")).unlink()
    else:
        bad.archive.unlink()
        if damage == "archive-link":
            bad.archive.symlink_to(job.path)
    if with_valid:
        job.create(2)

    assert cli(job) == 0
    out, err = capsys.readouterr()
    assert "Warnings:\n" in out
    assert f"  {bad.directory.name}:" in out
    assert expected in out and "Invalid entries: 1" in out
    assert f"Backups:         {int(with_valid)}" in out
    assert bad.archive.name not in out.split("Warnings:")[0]
    assert ("No backups found." in out) is not with_valid
    assert err == ""


def test_empty_destination_has_one_concise_report(job, capsys):
    (job.root / "unrelated").mkdir()
    assert cli(job) == 0
    out, err = capsys.readouterr()
    assert out == f"Vaultkeep — Backups\n\nDestination: {job.root}\n\nNo backups found.\n"
    assert err == ""


def test_verbose_paths_manifest_details_and_full_diagnostics(job, capsys):
    good = job.create(2)
    bad = job.create(1)
    metadata = json.loads(bad.manifest.read_text())
    metadata["hostname"] = 123
    bad.manifest.write_text(json.dumps(metadata))
    assert cli(job, "--verbose") == 0
    out, err = capsys.readouterr()
    assert sum(line.endswith(str(good.archive)) for line in out.splitlines()) == 1
    assert f"Directory: {good.directory}" in out
    assert f"Manifest: {good.manifest}" in out
    assert "Manifest version: 1" in out and "Size (bytes): 2048" in out
    assert "Hostname: host" in out and "Archive digest (stored): sha256:" in out
    assert str(bad.manifest) in out
    assert "hostname" in out and "Input should be a valid string" in out
    assert err == ""


@pytest.mark.parametrize("columns", [1, 25, 80, 300])
def test_narrow_terminals_keep_long_paths_using_verify_table_conventions(job, monkeypatch, columns):
    job.data["destination"]["name_template"] = (
        "a-long-archive-name-" + "x" * 70 + "-{timestamp_utc:%Y%m%dT%H%M%SZ}"
    )
    job.path.write_text(json.dumps(job.data))
    good = job.create(1)

    class Terminal(io.StringIO):
        @property
        def encoding(self):
            return "utf-8"

        def isatty(self):
            return True

        def fileno(self):
            return 1

    stream = Terminal()
    monkeypatch.setattr(
        output_module.os, "get_terminal_size", lambda fd: os.terminal_size((columns, 24))
    )
    monkeypatch.setattr("sys.stdout", stream)
    inventory = workflow.list_backups(job.path)
    print_inventory(inventory, output=Console())
    out = stream.getvalue()
    relative = str(good.archive.relative_to(job.root))
    assert relative in out and "..." not in out
    if columns < 300:
        assert "  Archive:" in out and "─" not in out
    else:
        assert "Created (UTC)" in out and "─" in out
    # Both commands use exactly the same column/fallback renderer.
    verify_table = format_table(
        ("Backup", "Manifest", "SHA-256", "Archive"), [(relative, "OK", "OK", "OK")], stream=stream
    )
    assert any("─" in line for line in verify_table) is (columns == 300)


def test_per_file_histories_share_parent_destination_and_sort_globally(job, capsys):
    from vaultkeep.file_backups import file_key

    job.data["backup_mode"] = "per_file"
    job.path.write_text(json.dumps(job.data))
    first = job.create(1, destination=job.root / file_key("old-source"))
    second = job.create(2, destination=job.root / file_key("deleted-source"))
    assert cli(job) == 0
    out, _ = capsys.readouterr()
    assert f"Destination: {job.root}" in out
    first_path, second_path = (str(item.archive.relative_to(job.root)) for item in (first, second))
    assert out.index(second_path) < out.index(first_path)
    assert "file-deleted-source-" in out and "Backups: 2" in out


@pytest.mark.parametrize("encoding", ["ascii", "utf-8"])
def test_unicode_paths_remain_identifiable_on_limited_terminals(job, monkeypatch, encoding):
    job.data["destination"]["name_template"] = "東京-é-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
    job.path.write_text(json.dumps(job.data))
    good = job.create(1)

    class EncodedOutput(io.StringIO):
        @property
        def encoding(self):
            return encoding

    stream = EncodedOutput()
    monkeypatch.setattr("sys.stdout", stream)
    assert cli(job) == 0
    relative = str(good.archive.relative_to(job.root))
    expected = relative.encode(encoding, errors="backslashreplace").decode(encoding)
    assert expected in stream.getvalue()
    assert "?" not in next(
        line for line in stream.getvalue().splitlines() if line.startswith("2026")
    )


@pytest.mark.parametrize("failure,expected", [("config", 3), ("destination", 5)])
def test_list_preserves_failure_exit_codes(job, capsys, failure, expected):
    if failure == "config":
        job.path.write_text("not: valid")
    else:
        job.root.rmdir()
    assert cli(job) == expected
    out, err = capsys.readouterr()
    assert out == "" and "FAILED:" in err
