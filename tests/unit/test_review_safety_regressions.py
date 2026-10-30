"""Regression cases for process safety, replacement metadata and cleanup outcomes."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.trigger.conftest import RecordingNotifier
from tests.unit import test_backup_safety_redesign as safety
from vaultkeep.backup import workflow
from vaultkeep.backup.status import backup_status
from vaultkeep.cli import status
from vaultkeep.destination import pruning
from vaultkeep.destination.discovery import discover_backups
from vaultkeep.destination.suspect import (
    SUSPECT_MARKER_NAME,
    mark_backup_suspect,
    read_suspect_marker,
    record_suspect_replacement,
)
from vaultkeep.errors import ArchiveChecksumMismatchError, DestinationError, PruneError
from vaultkeep.state.identity import job_identity_hash

job = safety.job


def _paths(job):
    return workflow.WorkflowPaths(job.parent / "state", job.parent / "cache", job.parent / "locks")


def _previous(job):
    return backup_status(_paths(job), "app", job_identity_hash(job, "app"))["previous"]


@pytest.mark.parametrize("backup_mode", ["combined", "per_file"])
@pytest.mark.parametrize("marker_writable", [True, False])
def test_new_archive_corrupt_during_cleanup_fails_execution(
    job, monkeypatch, backup_mode, marker_writable
):
    safety.configure(job, backup_mode=backup_mode)
    initial = safety.run(job)
    old = initial.archive_path if backup_mode == "combined" else initial.files[0].archive_path
    mark_backup_suspect(old.parent, "replace me")
    execute = workflow.execute_prune_plan
    corrupted = []

    def corrupt_before_recheck(plan, discovery):
        replacement = plan.replacements[0][1]
        corrupted.append(replacement.archive_path)
        replacement.archive_path.write_bytes(b"corrupted after commit")
        return execute(plan, discovery)

    monkeypatch.setattr(workflow, "execute_prune_plan", corrupt_before_recheck)
    if not marker_writable:

        def deny_marker(*args):
            raise DestinationError("cannot persist marker")

        monkeypatch.setattr(pruning, "mark_backup_suspect", deny_marker)
    if backup_mode == "combined":
        with pytest.raises(ArchiveChecksumMismatchError, match="Newly committed backup"):
            safety.run(job)
    else:
        result = safety.run(job)
        assert result.result == "failed"
        assert result.files[0].result == "failed"
    assert _previous(job)["result"] == "FAILED"
    assert old.is_file() and len(corrupted) == 1 and corrupted[0].is_file()
    assert (read_suspect_marker(corrupted[0].parent) is not None) == marker_writable


def test_sidecar_disagrees_with_manifest_marks_replacement_before_deletion(job, monkeypatch):
    old = safety.run(job).archive_path
    mark_backup_suspect(old.parent, "replace me")
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        replacement = safety.run(job).archive_path
    sidecar = replacement.with_suffix(replacement.suffix + ".sha256")
    sidecar.write_text("0" * 64 + f"  {replacement.name}\n")
    with pytest.raises(PruneError, match="immutable manifest"):
        workflow.prune_backups(job, dry_run=False, user_mode=True)
    assert old.is_file() and replacement.is_file()
    assert read_suspect_marker(replacement.parent) is not None


@pytest.mark.parametrize("missing", ["manifest", "identity", "both"])
def test_missing_version_fields_block_destructive_work(job, missing):
    archive = safety.run(job).archive_path
    manifest_path = next(archive.parent.glob("*.json"))
    document = json.loads(manifest_path.read_text())
    if missing in ("manifest", "both"):
        del document["manifest_version"]
    if missing in ("identity", "both"):
        del document["replacement_identity"]["identity_version"]
    manifest_path.write_text(json.dumps(document))
    before = {path: path.read_bytes() for path in archive.parent.iterdir()}
    discovery = discover_backups(workflow.load_validated_config(job), metadata_only=True)
    assert not discovery.backups and len(discovery.malformed) == 1
    for dry_run in (True, False):
        with pytest.raises(PruneError, match="malformed"):
            workflow.prune_backups(job, dry_run=dry_run, user_mode=True)
    assert {path: path.read_bytes() for path in archive.parent.iterdir()} == before


def test_backward_clock_replacement_remains_usable_and_can_be_cleaned(job, monkeypatch):
    now = datetime(2026, 10, 27, tzinfo=UTC)
    monkeypatch.setattr(workflow, "datetime", SimpleNamespace(now=lambda: now))
    old = safety.run(job).archive_path
    mark_backup_suspect(old.parent, "original evidence")
    original = read_suspect_marker(old.parent)
    now -= timedelta(days=2)
    with monkeypatch.context() as patch:

        def deny_delete(backup):
            raise PruneError("delete denied")

        patch.setattr(pruning, "_remove_valid_backup", deny_delete)
        result = safety.run(job)
    assert result.result == "created" and result.cleanup_errors
    discovery = discover_backups(workflow.load_validated_config(job), metadata_only=True)
    suspect = next(item for item in discovery.backups if item.archive_path == old)
    replacement = pruning.valid_replacement(suspect, discovery)
    assert replacement is not None
    assert replacement.manifest.created_datetime_utc < suspect.manifest.created_datetime_utc
    marker = read_suspect_marker(old.parent)
    assert (marker.reason, marker.detected_at) == (original.reason, original.detected_at)
    assert safety.run(job).result == "unchanged"
    assert workflow.prune_backups(job, dry_run=False, user_mode=True).removed == 1
    assert not old.exists() and result.archive_path.is_file()


def _two_archives(job, monkeypatch):
    first = safety.run(job).archive_path
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        (job.parent / "source/data").write_bytes(b"second")
        second = safety.run(job).archive_path
    return first, second


def test_circular_suspect_references_preserve_both_until_fresh_replacement(job, monkeypatch):
    archives = _two_archives(job, monkeypatch)
    config = workflow.load_validated_config(job)
    discovery = discover_backups(config, metadata_only=True)
    ids = {item.archive_path: item.manifest.backup_id for item in discovery.backups}
    for index, archive in enumerate(archives):
        mark_backup_suspect(archive.parent, "original")
        record_suspect_replacement(archive.parent, ids[archives[1 - index]])
    assert workflow.prune_backups(job, dry_run=False, user_mode=True).removed == 0
    assert all(path.is_file() for path in archives)
    result = safety.run(job)
    assert result.result == "created" and not result.cleanup_errors
    assert all(not path.exists() for path in archives)
    assert result.archive_path.is_file()


def test_duplicate_replacement_identity_blocks_every_deletion(job, monkeypatch):
    old = safety.run(job).archive_path
    mark_backup_suspect(old.parent, "original")
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        replacement = safety.run(job).archive_path
    base_name = next(replacement.parent.glob("*.json")).stem
    duplicate = replacement.parent.with_name(base_name + "-99")
    shutil.copytree(replacement.parent, duplicate)
    assert (
        len(discover_backups(workflow.load_validated_config(job), metadata_only=True).backups) == 3
    )
    with pytest.raises(PruneError, match="Duplicate"):
        workflow.prune_backups(job, dry_run=False, user_mode=True)
    assert old.is_file() and replacement.is_file() and duplicate.is_dir()


def test_interrupted_multi_marker_update_preserves_original_evidence_and_all_archives(
    job, monkeypatch
):
    archives = _two_archives(job, monkeypatch)
    for archive in archives:
        mark_backup_suspect(archive.parent, "original evidence")
    originals = [read_suspect_marker(path.parent) for path in archives]
    record = workflow.record_suspect_replacement
    calls = []

    def interrupted(directory, backup_id):
        calls.append(directory)
        if len(calls) == 2:
            raise DestinationError("second marker update interrupted")
        record(directory, backup_id)

    with monkeypatch.context() as patch:
        patch.setattr(workflow, "record_suspect_replacement", interrupted)
        patch.setattr(workflow, "execute_prune_plan", lambda *args: pytest.fail("unsafe cleanup"))
        result = safety.run(job)
    assert result.result == "created" and result.cleanup_errors
    markers = [read_suspect_marker(path.parent) for path in archives]
    assert sum(marker.replacement_backup_id is not None for marker in markers) == 1
    assert [(marker.reason, marker.detected_at) for marker in markers] == [
        (marker.reason, marker.detected_at) for marker in originals
    ]
    assert all(path.is_file() for path in archives) and result.archive_path.is_file()
    assert safety.run(job).result == "created"
    assert all(not path.exists() for path in archives)
    assert safety.run(job).result == "unchanged"


def test_status_distinguishes_cleanup_pending_from_unresolved_without_writes(job, monkeypatch):
    old = safety.run(job).archive_path
    mark_backup_suspect(old.parent, "original")
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        replacement = safety.run(job).archive_path
    marker_path = old.parent / SUSPECT_MARKER_NAME
    before = (marker_path.read_bytes(), marker_path.stat().st_mtime_ns)
    monkeypatch.setattr(status, "user_workflow_paths", lambda: _paths(job))
    pending = status.collect_status(job, user_mode=True, runtime_params={})
    assert len(pending["destination"]["cleanup_pending"]) == 1
    assert pending["destination"]["unresolved_suspects"] == []
    assert pending["backup"]["previous"]["cleanup_errors"] == []
    shutil.rmtree(replacement.parent)
    unresolved = status.collect_status(job, user_mode=True, runtime_params={})
    assert unresolved["destination"]["cleanup_pending"] == []
    assert len(unresolved["destination"]["unresolved_suspects"]) == 1
    assert (marker_path.read_bytes(), marker_path.stat().st_mtime_ns) == before


def test_cleanup_warning_reaches_notification_and_execution_status(job, monkeypatch):
    notifier = RecordingNotifier()
    monkeypatch.setattr("vaultkeep.backup.notifications.build_notifier", lambda config: notifier)
    safety.configure(job, desktop_notifications={"backup": {"enabled": True}})

    def fail_cleanup(*args):
        raise PruneError("delete denied")

    monkeypatch.setattr(workflow, "execute_prune_plan", fail_cleanup)
    result = safety.run(job)
    assert result.result == "created" and result.cleanup_errors
    previous = _previous(job)
    assert previous["result"] == "SUCCESS" and previous["cleanup_errors"]
    title, body = notifier.calls[-1]
    assert title == "VaultKeep: Backup completed"
    assert "new archive created" in body and "cleanup failed:" in body and "delete denied" in body


def test_unknown_execution_has_empty_cleanup_errors(job):
    safety.run(job)
    history = next((job.parent / "state").rglob("execution.json"))
    history.write_text("malformed")
    previous = _previous(job)
    assert previous["result"] == "UNKNOWN" and previous["cleanup_errors"] == []
