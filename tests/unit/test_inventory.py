"""Read-only inventory facts and presentation shared with verification."""

from __future__ import annotations

import io
import json
import os
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaultkeep.archive.checksums import calculate_archive_sha256, write_checksum_sidecar
from vaultkeep.backup import workflow
from vaultkeep.cli import commands
from vaultkeep.cli import output as output_module
from vaultkeep.cli.inventory import print_inventory
from vaultkeep.cli.output import Console
from vaultkeep.destination.damage import mark_backup_damaged
from vaultkeep.destination.discovery import discover_backups
from vaultkeep.destination.manifest import BackupManifest, write_manifest
from vaultkeep.destination.pruning import build_prune_plan
from vaultkeep.destination.templates import allocate_job_backup_paths
from vaultkeep.errors import ConfigurationError
from vaultkeep.file_backups import destination_config, file_key


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


def records(text):
    """Read the table row paired with each displayed backup directory."""
    result = {}
    row = None
    for line in text.splitlines():
        if line.startswith("2026-"):
            row = re.split(r" {2,}", line.strip())
        elif line.startswith("  Backup:"):
            result[line.removeprefix("  Backup: ")] = row
    return result


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
    assert out.startswith(f"Vaultkeep — Backups\n\nJob:         app\nDestination: {job.root}\n")
    assert "Created (UTC)" in out and "Enc" in out
    assert "2026-10-24 05:05:18  1.5 KiB  no" in out
    assert "2026-10-23 02:00:00  2.0 KiB  yes" in out
    paths = [
        f"  Backup: {item.directory.relative_to(job.root)}" for item in (newest, encrypted, first)
    ]
    assert out.index(paths[0]) < out.index(paths[1]) < out.index(paths[2])
    assert str(newest.archive) not in out
    assert unrelated.directory.name not in out
    assert "Backups: 3 · Keep: 3 · Prune: 0" in out
    assert newest.archive.name not in out and "Metadata:" not in out
    assert "VERIFIED" not in out and "Result:" not in out
    assert "list: listed" not in out and "SUCCESS" not in out
    assert "[00:" not in out and err == ""


def test_inventory_marks_damaged_backups_and_preserves_healthy_retention_preview(job, capsys):
    healthy = job.create(1)
    damaged = job.create(2)
    mark_backup_damaged(damaged.directory, "checksum mismatch")
    before = snapshot(job.path.parent)

    assert cli(job) == 0

    out, err = capsys.readouterr()
    rows = records(out)
    assert rows[str(damaged.directory.relative_to(job.root))][3] == "DAMAGED"
    assert rows[str(healthy.directory.relative_to(job.root))][3] == "KEEP"
    assert "checksum mismatch" in out
    assert "Damaged backups are preserved and excluded from retention." in out
    assert "Damaged: 1" in out
    assert "UNKNOWN" not in out
    assert err == ""
    assert snapshot(job.path.parent) == before


def test_inventory_never_reads_contents_or_changes_backups_config_or_state(
    job, monkeypatch, capsys
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
        assert cli(job) == 0
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
    assert f"Backups: {int(with_valid)}" in out
    if with_valid:
        assert "Keep: 0 · Prune: 0 · Unknown: 1" in out
    assert bad.archive.name not in out.split("Warnings:")[0]
    assert ("No backups found." in out) is not with_valid
    assert err == ""


def test_empty_destination_has_one_concise_report(job, capsys):
    (job.root / "unrelated").mkdir()
    assert cli(job) == 0
    out, err = capsys.readouterr()
    assert "No backups found.\n\nBackups: 0\nNo backups are deleted.\n" in out
    assert "Retention:   24 hourly, 7 daily, 8 weekly, 12 monthly, 3 yearly" in out
    assert err == ""


def test_full_warning_diagnostics_without_verbose_mode(job, capsys):
    good = job.create(2)
    bad = job.create(1)
    metadata = json.loads(bad.manifest.read_text())
    metadata["hostname"] = 123
    bad.manifest.write_text(json.dumps(metadata))
    assert cli(job) == 0
    out, err = capsys.readouterr()
    assert f"  Backup: {good.directory.relative_to(job.root)}" in out
    assert str(good.archive) not in out and good.archive.name not in out
    assert "hostname" in out and "Input should be a valid string" in out
    assert "Retention preview incomplete" in out and "UNKNOWN" in out
    assert err == ""


@pytest.mark.parametrize("columns", [1, 25, 80, 300])
def test_narrow_terminals_keep_long_paths_using_verify_table_conventions(job, monkeypatch, columns):
    job.data["destination"]["name_template"] = (
        "a-long-archive-name-" + "x" * 70 + "-{timestamp_utc:%Y%m%dT%H%M%SZ}"
    )
    job.data["retention"] = {"hourly": 1, "daily": 0, "weekly": 0, "monthly": 0, "yearly": 0}
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
    relative = str(good.directory.relative_to(job.root))
    assert relative in out and "..." not in out
    assert f"  Backup: {relative}" in out
    assert good.archive.name not in out
    if columns < 80:
        assert "  Decision:" in out and "─" not in out
    else:
        assert "Created (UTC)" in out and "─" in out
        separator = next(line for line in out.splitlines() if line.startswith("─"))
        assert len(separator) < 80
        assert len(relative) > len(separator)


def test_per_file_histories_share_parent_destination_and_sort_globally(job, capsys):
    from vaultkeep.file_backups import file_key

    job.data["backup_mode"] = "per_file"
    job.path.write_text(json.dumps(job.data))
    first = job.create(1, destination=job.root / file_key("old-source"))
    second = job.create(2, destination=job.root / file_key("deleted-source"))
    assert cli(job) == 0
    out, _ = capsys.readouterr()
    assert f"Destination: {job.root}" in out
    first_path, second_path = (
        str(item.directory.relative_to(job.root)) for item in (first, second)
    )
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
    relative = str(good.directory.relative_to(job.root))
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


def test_retention_tiers_and_prune_decisions_agree_with_existing_planner(job, capsys):
    job.data["retention"] = {"hourly": 1, "daily": 2, "weekly": 0, "monthly": 0, "yearly": 0}
    job.path.write_text(json.dumps(job.data))
    oldest = job.create(1, created=datetime(2026, 10, 22, 8, tzinfo=UTC))
    redundant = job.create(2, created=datetime(2026, 10, 23, 8, tzinfo=UTC))
    newest = job.create(3, created=datetime(2026, 10, 23, 9, tzinfo=UTC))
    # Use complete checksum metadata here so the ordinary prune discovery path
    # sees exactly the same inventory as the lightweight listing path.
    for backup in (oldest, redundant, newest):
        checksum = next(backup.directory.glob("*.sha256"))
        checksum.unlink()
        digest = calculate_archive_sha256(backup.archive)
        write_checksum_sidecar(backup.archive, checksum, digest)
        metadata = json.loads(backup.manifest.read_text())
        metadata["archive_digest"] = f"sha256:{digest}"
        backup.manifest.write_text(json.dumps(metadata))
    config = workflow.load_validated_config(job.path)
    discovery = discover_backups(config)
    plan = build_prune_plan(discovery, config.retention).retention
    before = snapshot(job.path.parent)

    assert cli(job) == 0
    out, err = capsys.readouterr()
    displayed = records(out)
    assert displayed[str(newest.directory.relative_to(job.root))][3:] == ["KEEP", "hourly, daily"]
    assert displayed[str(oldest.directory.relative_to(job.root))][3:] == ["KEEP", "daily"]
    assert displayed[str(redundant.directory.relative_to(job.root))][3:] == ["PRUNE", "—"]
    for backup in discovery.backups:
        record = displayed[str(backup.directory.relative_to(job.root))]
        tiers = {
            tier
            for tier, selected in plan.selections.items()
            if backup.manifest.backup_id in {item.backup_id for item in selected}
        }
        assert record[3] == ("KEEP" if tiers else "PRUNE")
        assert set(record[4].split(", ")) == (tiers or {"—"})
    assert "Backups: 3 · Keep: 2 · Prune: 1" in out
    assert "PRUNE indicates eligibility under the current retention policy." in out
    assert "No backups are deleted." in out and err == ""
    assert workflow.prune_backups(job.path, dry_run=True).result == "planned"
    assert snapshot(job.path.parent) == before


@pytest.mark.parametrize("invalid_history", [False, True])
def test_per_file_retention_uses_independent_histories(job, capsys, invalid_history):
    job.data["backup_mode"] = "per_file"
    job.data["retention"] = {"hourly": 0, "daily": 1, "weekly": 0, "monthly": 0, "yearly": 0}
    job.path.write_text(json.dumps(job.data))
    first_root, second_root = (job.root / file_key(name) for name in ("one", "two"))
    backups = [
        job.create(1, destination=first_root),
        job.create(2, destination=first_root),
        job.create(3, destination=second_root),
        job.create(4, destination=second_root),
    ]
    if invalid_history:
        job.create(5, destination=first_root).manifest.unlink()
    assert cli(job) == 0
    out, _ = capsys.readouterr()
    displayed = records(out)
    assert [displayed[str(backup.directory.relative_to(job.root))][3] for backup in backups] == (
        ["UNKNOWN", "UNKNOWN", "PRUNE", "KEEP"]
        if invalid_history
        else ["PRUNE", "KEEP", "PRUNE", "KEEP"]
    )
    if invalid_history:
        assert f"Retention preview incomplete for {first_root.name}" in out
        assert "Keep: 1 · Prune: 1 · Unknown: 2" in out
        assert "Matching invalid entries block pruning" in out
    else:
        config = workflow.load_validated_config(job.path)
        for root in (first_root, second_root):
            plan = build_prune_plan(
                discover_backups(destination_config(config, root), metadata_only=True),
                config.retention,
            ).retention
            assert len(plan.retain) == len(plan.delete) == 1
        assert "Keep: 2 · Prune: 2" in out


@pytest.mark.parametrize("invalid", [False, True])
def test_disabled_retention_is_display_only_and_does_not_relax_other_validation(
    job, capsys, monkeypatch, invalid
):
    good = job.create(1)
    if invalid:
        job.create(2).manifest.unlink()
    job.data["retention"] = dict.fromkeys(job.data["retention"], 0)
    job.path.write_text(json.dumps(job.data))

    def forbidden(*args, **kwargs):
        pytest.fail("Disabled retention must not invoke the planner")

    monkeypatch.setattr(workflow, "build_prune_plan", forbidden)
    before = snapshot(job.path.parent)
    assert cli(job) == 0
    out, _ = capsys.readouterr()
    assert "Retention:   disabled" in out
    assert records(out)[str(good.directory.relative_to(job.root))][3:] == ["—", "—"]
    assert "Keep:" not in out and "Prune:" not in out and "Unknown:" not in out
    assert snapshot(job.path.parent) == before
    with pytest.raises(ConfigurationError, match="At least one retention tier"):
        workflow.load_validated_config(job.path)
    assert commands.main(["--config", str(job.path), "prune", "--dry-run"]) == 3
    job.data["destination"]["root"] = "relative"
    job.path.write_text(json.dumps(job.data))
    assert cli(job) == 3
    _, errors = capsys.readouterr()
    assert "Destination root must be absolute" in errors


def test_duplicate_backup_ids_make_preview_unknown_without_hiding_inventory(job, capsys):
    first, second = job.create(1), job.create(2)
    metadata = json.loads(second.manifest.read_text())
    metadata["backup_id"] = json.loads(first.manifest.read_text())["backup_id"]
    second.manifest.write_text(json.dumps(metadata))
    assert cli(job) == 0
    out, _ = capsys.readouterr()
    assert {row[3] for row in records(out).values()} == {"UNKNOWN"}
    assert "Duplicate backup ID" in out and "Unknown: 2" in out


def test_retention_uses_stored_local_calendar_days_while_displaying_utc(job, capsys):
    job.data["retention"] = {"hourly": 0, "daily": 2, "weekly": 0, "monthly": 0, "yearly": 0}
    job.path.write_text(json.dumps(job.data))
    zone = timezone(timedelta(hours=3))
    first = job.create(1, created=datetime(2026, 10, 23, 0, 30, tzinfo=zone))
    second = job.create(2, created=datetime(2026, 10, 23, 23, 30, tzinfo=zone))
    assert cli(job) == 0
    out, _ = capsys.readouterr()
    displayed = records(out)
    # Two different UTC dates share one stored local calendar day. Only its
    # newest recovery point is selected even with two daily buckets configured.
    assert displayed[str(first.directory.relative_to(job.root))][0] == "2026-10-22 21:30:00"
    assert displayed[str(first.directory.relative_to(job.root))][3] == "PRUNE"
    assert displayed[str(second.directory.relative_to(job.root))][3:] == ["KEEP", "daily"]


def test_list_has_no_verbose_option(job, capsys):
    with pytest.raises(SystemExit) as caught:
        cli(job, "--verbose")
    assert caught.value.code == 2
    assert "unrecognized arguments: --verbose" in capsys.readouterr().err
