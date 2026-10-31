"""Deletion intent survives failures without becoming recovery evidence."""

from __future__ import annotations

import errno
import json
import os

import pytest

from tests.helpers.backup import configure, run
from tests.helpers.backup import job as job
from vaultkeep.backup import workflow
from vaultkeep.backup.results import failure_exit_code
from vaultkeep.cli.commands import main
from vaultkeep.destination import pending as pending_module
from vaultkeep.destination.discovery import discover_backups
from vaultkeep.destination.pending import (
    PRUNE_MARKER_NAME,
    TEMP_DIRECTORY_NAME,
    PendingBackup,
    PruneMarker,
    opened_directory,
    publish_marker,
    read_marker,
)
from vaultkeep.destination.pruning import build_prune_plan, execute_prune_plan
from vaultkeep.destination.suspect import mark_backup_suspect, read_suspect_marker
from vaultkeep.errors import PruneError
from vaultkeep.locking import destination_lock


def discovery(job):
    return discover_backups(workflow.load_validated_config(job), metadata_only=True)


def change(job):
    source = job.parent / "source" / "data"
    source.write_bytes(source.read_bytes() + b"changed")


def intent(job, directory):
    backup = next(b for b in discovery(job).backups if b.directory == directory)
    return PruneMarker(
        version=1,
        directory_name=directory.name,
        manifest=backup.manifest,
        suspect=read_suspect_marker(directory),
    )


def publish(job, directory):
    marker = intent(job, directory)
    with destination_lock(directory.parent), opened_directory(directory) as (parent, descriptor):
        publish_marker(parent, descriptor, marker)
    return marker


@pytest.mark.parametrize(
    "failure", ["archive", "checksum", "manifest", "suspect", "marker", "rmdir"]
)
def test_failures_stop_once_and_resume_after_new_success(job, monkeypatch, failure):
    old = run(job).archive_path
    if failure == "suspect":
        mark_backup_suspect(old.parent, "original diagnostic")
    original_unlink, original_rmdir = os.unlink, os.rmdir
    targets = {
        "archive": old.name,
        "checksum": old.name + ".sha256",
        "manifest": next(old.parent.glob("*.json")).name,
        "suspect": ".vaultkeep-suspect",
        "marker": PRUNE_MARKER_NAME,
    }
    attempts = []

    def unlink(path, *args, **kwargs):
        if str(path) == targets.get(failure):
            attempts.append(path)
            raise PermissionError(errno.EACCES, "synchronization busy", str(path))
        return original_unlink(path, *args, **kwargs)

    def rmdir(path, *args, **kwargs):
        if str(path) == old.parent.name:
            attempts.append(path)
            raise OSError(errno.ENOTEMPTY, "synchronization busy", str(path))
        return original_rmdir(path, *args, **kwargs)

    change(job)
    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", unlink)
        if failure == "rmdir":
            patch.setattr(os, "rmdir", rmdir)
        created = run(job)
    assert created.result == "created" and created.cleanup_errors
    assert len(attempts) == 1
    assert old.parent.exists()
    classified = discovery(job)
    if failure == "rmdir":
        assert classified.orphans == (old.parent,)
        assert not list(old.parent.iterdir())
    else:
        assert len(classified.pending) == 1 and not classified.malformed
        assert (old.parent / PRUNE_MARKER_NAME).is_file()
    remaining_names = {p.name for p in old.parent.iterdir()}
    expected = {
        "archive": {old.name, targets["checksum"], targets["manifest"], PRUNE_MARKER_NAME},
        "checksum": {targets["checksum"], targets["manifest"], PRUNE_MARKER_NAME},
        "manifest": {targets["manifest"], PRUNE_MARKER_NAME},
        "suspect": {".vaultkeep-suspect", PRUNE_MARKER_NAME},
        "marker": {PRUNE_MARKER_NAME},
        "rmdir": set(),
    }
    assert remaining_names == expected[failure]
    assert not any(b.directory == old.parent for b in classified.backups)
    # UNCHANGED does not perform a cleanup attempt.
    assert run(job).result == "unchanged"
    assert old.parent.exists()
    change(job)
    assert run(job).result == "created"
    assert not old.parent.exists()


@pytest.mark.parametrize("stage", ["publish", "file_flush", "directory_flush"])
def test_failed_publication_never_deletes_artifacts(job, monkeypatch, stage):
    old = run(job).archive_path
    before = {p.name: p.read_bytes() for p in old.parent.iterdir()}
    original_fsync = os.fsync

    def fail(*args):
        raise OSError(errno.EIO, "publication failed")

    def fsync(fd):
        import stat

        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if directory == (stage == "directory_flush"):
            fail()
        original_fsync(fd)

    change(job)
    with monkeypatch.context() as patch:
        if stage == "publish":
            patch.setattr(pending_module, "publish_noreplace", fail)
        else:
            # Limit faults to marker publication, not backup creation/state saving.
            original_publish = pending_module.publish_marker

            def broken_publish(*args):
                with monkeypatch.context() as inner:
                    inner.setattr(os, "fsync", fsync)
                    original_publish(*args)

            patch.setattr(pending_module, "publish_marker", broken_publish)
        result = run(job)
    assert result.result == "created" and result.cleanup_errors
    for name, content in before.items():
        assert (old.parent / name).read_bytes() == content
    if stage != "directory_flush":
        assert set(p.name for p in old.parent.iterdir()) == set(before)
        assert any(b.directory == old.parent for b in discovery(job).backups)
    assert (old.parent.parent / TEMP_DIRECTORY_NAME).is_dir()


@pytest.mark.parametrize(
    "unexpected", ["file", "directory", "link", "marker-link", "bad-marker", "missing-marker"]
)
def test_unknown_pending_contents_block_cleanup(job, monkeypatch, unexpected):
    old = run(job).archive_path
    change(job)
    # Preserve old backup long enough to publish a controlled interrupted state.
    config = configure(
        job, retention={"hourly": 0, "daily": 2, "weekly": 0, "monthly": 0, "yearly": 0}
    )
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        survivor = run(job).archive_path
    publish(job, old.parent)
    old.unlink()
    marker = old.parent / PRUNE_MARKER_NAME
    if unexpected == "file":
        (old.parent / "foreign").write_text("preserve")
    elif unexpected == "directory":
        (old.parent / "foreign").mkdir()
    elif unexpected == "link":
        (old.parent / "foreign").symlink_to(survivor)
    elif unexpected == "marker-link":
        marker.unlink()
        marker.symlink_to(survivor)
    elif unexpected == "bad-marker":
        marker.write_text("{}")
    else:
        marker.unlink()
    facts = discovery(job)
    assert facts.malformed and not facts.pending
    with pytest.raises(PruneError, match="malformed"):
        build_prune_plan(facts, config.retention)
    assert survivor.is_file()


def test_actual_corrupt_survivor_aborts_before_candidate_marker(job, monkeypatch):
    old = run(job).archive_path
    configure(job, retention={"hourly": 0, "daily": 2, "weekly": 0, "monthly": 0, "yearly": 0})
    change(job)
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        survivor = run(job).archive_path
    survivor.write_bytes(b"changed without changing metadata")
    facts = discovery(job)
    plan = build_prune_plan(facts, workflow.load_validated_config(job).retention)
    assert old.parent in [b.directory for b in plan.backups_to_delete]
    with pytest.raises(PruneError, match="corrupt"):
        execute_prune_plan(plan, facts)
    assert old.is_file() and not (old.parent / PRUNE_MARKER_NAME).exists()


@pytest.mark.parametrize("loss", ["missing", "corrupt"])
def test_pending_suspect_lost_replacement_forces_creation_on_unchanged_sources(
    job, monkeypatch, loss
):
    old = run(job).archive_path
    mark_backup_suspect(old.parent, "suspect")
    original = os.unlink

    def fail_archive(path, *args, **kwargs):
        if str(path) == old.name:
            raise PermissionError("busy")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail_archive)
        replacement = run(job).archive_path
    assert discovery(job).pending
    if loss == "missing":
        # Loss of the whole replacement leaves no unknown malformed directory.
        for path in replacement.parent.iterdir():
            path.unlink()
        replacement.parent.rmdir()
    else:
        replacement.write_bytes(b"corrupt")
    result = run(job)
    assert result.result == "created"
    assert result.archive_path.is_file()
    assert not old.parent.exists()


@pytest.mark.parametrize("field", ["reason", "detected_at", "replacement_backup_id"])
def test_complete_original_suspect_snapshot_is_revalidated(job, field):
    old = run(job).archive_path
    mark_backup_suspect(old.parent, "diagnostic")
    marker = publish(job, old.parent)
    original = marker.suspect.model_dump()
    original[field] = {
        "reason": "changed",
        "detected_at": "2020-01-01T00:00:00Z",
        "replacement_backup_id": "f" * 32,
    }[field]
    (old.parent / ".vaultkeep-suspect").write_text(json.dumps(original))
    assert discovery(job).malformed


@pytest.mark.parametrize("race", [False, True])
def test_atomic_publication_never_overwrites_existing_marker(job, monkeypatch, race):
    old = run(job).archive_path
    marker = intent(job, old.parent)
    payload = b"existing competing intent"
    path = old.parent / PRUNE_MARKER_NAME
    original = pending_module.publish_noreplace

    def competing(source, name, target):
        path.write_bytes(payload)
        original(source, name, target)

    if race:
        monkeypatch.setattr(pending_module, "publish_noreplace", competing)
    else:
        path.write_bytes(payload)
    with (
        destination_lock(old.parent.parent),
        opened_directory(old.parent) as (parent, descriptor),
        pytest.raises((PruneError, FileExistsError)),
    ):
        publish_marker(parent, descriptor, marker)
    assert path.read_bytes() == payload and old.is_file()


def test_empty_orphan_and_dry_run_are_safe(job):
    archive = run(job).archive_path
    orphan = archive.parent.with_name(archive.parent.name + "-2")
    orphan.mkdir()
    before = {p: p.stat().st_mtime_ns for p in orphan.parent.rglob("*")}
    assert workflow.prune_backups(job, dry_run=True, user_mode=True).result == "planned"
    assert {p: p.stat().st_mtime_ns for p in orphan.parent.rglob("*")} == before
    assert workflow.prune_backups(job, dry_run=False, user_mode=True).removed == 1
    assert not orphan.exists() and archive.is_file()


@pytest.mark.parametrize("kind", ["pending-only", "mixed", "malformed", "corrupt"])
def test_verify_contract_and_classifications(job, monkeypatch, capsys, kind):
    old = run(job).archive_path
    marker = publish(job, old.parent)
    if kind != "pending-only":
        # Creating a backup would automatically resume cleanup; use the normal
        # backup path while suppressing only execution of the cleanup plan.
        change(job)
        with monkeypatch.context() as patch:
            patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
            survivor = run(job).archive_path
        if kind == "malformed":
            (survivor.parent / "unknown").write_text("preserve")
        elif kind == "corrupt":
            survivor.write_bytes(b"bad actual archive")
    assert main(["--user", "--config", str(job), "list"]) == 0
    listed = capsys.readouterr().out
    assert "PRUNE_PENDING" in listed and "VERIFIED" not in listed
    if kind == "mixed":
        assert "ELIGIBLE" in listed and "KEEP" in listed
    code = main(["--user", "--config", str(job), "verify"])
    output = capsys.readouterr()
    assert code == {"pending-only": 5, "mixed": 0, "malformed": 5, "corrupt": 7}[kind]
    assert "PRUNE_PENDING" in output.out
    assert (
        "Result:    VERIFIED" in output.out
        if kind == "mixed"
        else "Result:    FAILED" in output.out
    )
    assert (old.parent / PRUNE_MARKER_NAME).is_file()
    with opened_directory(old.parent) as (_, descriptor):
        assert read_marker(descriptor) == marker


def test_pending_identity_filtering_preserves_visibility(job):
    old = run(job).archive_path
    publish(job, old.parent)
    configure(job, exclude=["different"])
    facts = workflow._active_history(
        discovery(job), workflow.load_validated_config(job), application=None
    )
    assert len(facts.pending) == 1 and not facts.pending[0].active
    assert build_prune_plan(facts, workflow.load_validated_config(job).retention).pending == ()
    assert workflow.prune_backups(job, dry_run=False, user_mode=True).removed == 0
    assert old.is_file()


def test_duplicate_ids_across_two_pending_entries_block_prune(job):
    old = run(job).archive_path
    marker = publish(job, old.parent)
    collision = old.parent.with_name(old.parent.name + "-2")
    collision.mkdir()
    duplicate = marker.model_copy(update={"directory_name": collision.name})
    (collision / PRUNE_MARKER_NAME).write_text(duplicate.model_dump_json())
    facts = discovery(job)
    assert len(facts.pending) == 2
    with pytest.raises(PruneError, match="Duplicate"):
        build_prune_plan(facts, workflow.load_validated_config(job).retention)


def test_pending_alone_never_satisfies_retention_or_survivor_guard(job):
    old = run(job).archive_path
    marker = publish(job, old.parent)
    facts = discovery(job)
    assert facts.state_records == () and facts.backups == ()
    with pytest.raises(PruneError, match="recovery point") as error:
        build_prune_plan(facts, workflow.load_validated_config(job).retention)
    assert failure_exit_code(error.value) == 5
    assert old.is_file() and facts.pending == (PendingBackup(old.parent, marker),)


def test_status_preserves_backup_success_with_pending_cleanup(job, monkeypatch):
    from vaultkeep.backup.status import backup_status
    from vaultkeep.cli.status import collect_status
    from vaultkeep.state.identity import job_identity_hash

    old = run(job).archive_path
    original = os.unlink

    def fail(path, *args, **kwargs):
        if str(path) == old.name:
            raise PermissionError("busy")
        return original(path, *args, **kwargs)

    change(job)
    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail)
        result = run(job)
    assert result.cleanup_errors
    report = backup_status(
        workflow.WorkflowPaths(job.parent / "state", job.parent / "cache", job.parent / "locks"),
        "app",
        job_identity_hash(job, "app"),
    )
    assert report["previous"]["result"] == "SUCCESS"
    assert report["previous"]["cleanup_errors"]
    status = collect_status(job, user_mode=True, runtime_params={})
    assert status["destination"]["cleanup_pending"][0]["classification"] == "PRUNE_PENDING"


@pytest.mark.parametrize("mutation", ["archive-name", "directory", "job", "version", "oversized"])
def test_marker_cannot_authorize_arbitrary_layout_or_history(job, mutation):
    archive = run(job).archive_path
    marker = publish(job, archive.parent)
    data = marker.model_dump(mode="json")
    if mutation == "archive-name":
        data["manifest"]["archive"] = "../outside.tar.zst"
    elif mutation == "directory":
        data["directory_name"] = "another-directory"
    elif mutation == "job":
        data["manifest"]["job"] = "other"
    else:
        data["version"] = 99
    path = archive.parent / PRUNE_MARKER_NAME
    path.write_text("x" * (1024 * 1024 + 1) if mutation == "oversized" else json.dumps(data))
    facts = discovery(job)
    assert facts.malformed and not facts.backups and not facts.pending
    assert archive.is_file()


def test_orphan_became_nonempty_and_directory_swap_are_refused(job):
    from vaultkeep.destination.pending import check_directory, remove_orphan

    archive = run(job).archive_path
    orphan = archive.parent.with_name(archive.parent.name + "-2")
    orphan.mkdir()
    facts = discovery(job)
    plan = build_prune_plan(facts, workflow.load_validated_config(job).retention)
    (orphan / "foreign").write_text("keep")
    with pytest.raises(PruneError, match="no longer empty"):
        execute_prune_plan(plan, facts)
    assert (orphan / "foreign").read_text() == "keep"
    with destination_lock(orphan.parent), opened_directory(orphan) as (parent, descriptor):
        moved = orphan.with_name("operator-directory")
        orphan.rename(moved)  # External change, never a VaultKeep recovery strategy.
        orphan.symlink_to(moved, target_is_directory=True)
        with pytest.raises(PruneError, match="identity changed"):
            check_directory(parent, descriptor, orphan.name)
        with pytest.raises(PruneError):
            remove_orphan(orphan)
    assert (moved / "foreign").read_text() == "keep"


@pytest.mark.parametrize("mode", ["excluded", "deleted", "symlink"])
def test_pending_per_file_history_stays_visible_but_inactive(job, monkeypatch, capsys, mode):
    from vaultkeep.file_backups import file_config

    configure(job, backup_mode="per_file")
    (job.parent / "source" / "second").write_text("independent")
    result = run(job)
    old = result.files[0].archive_path
    parent_config = workflow.load_validated_config(job)
    # Build intent using the actual single-file discovery configuration.
    source = result.files[0].source
    child_config = file_config(parent_config, source)
    backup = discover_backups(child_config, metadata_only=True).backups[0]
    marker = PruneMarker(version=1, directory_name=old.parent.name, manifest=backup.manifest)
    with destination_lock(old.parent.parent), opened_directory(old.parent) as (parent, descriptor):
        publish_marker(parent, descriptor, marker)
    if mode == "excluded":
        configure(job, exclude=[source.name])
    else:
        source.unlink()
        if mode == "symlink":
            source.symlink_to(job)
    orphan = old.parent.with_name(old.parent.name + "-2")
    orphan.mkdir()
    assert workflow.prune_backups(job, dry_run=False, user_mode=True).removed == 0
    assert orphan.is_dir()
    inventory = workflow.list_backups(job, user_mode=True)
    assert len(inventory.pending) == 1 and not inventory.pending[0].active
    assert old.is_file()
    assert main(["--user", "--config", str(job), "verify"]) == 5
    output = capsys.readouterr().out
    assert "PRUNE_PENDING: 1" in output and "Verified:  1/3 backups" in output
    assert "Result:    FAILED" in output


def test_failure_stops_later_candidates_and_repeated_failure_is_deferred(job, monkeypatch):
    run(job)
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        change(job)
        run(job)
        change(job)
        run(job)
    facts = discovery(job)
    plan = build_prune_plan(facts, workflow.load_validated_config(job).retention)
    assert len(plan.backups_to_delete) == 2
    first, later = plan.backups_to_delete
    before = {p: p.read_bytes() for p in later.directory.iterdir()}
    attempts = []
    original = os.unlink

    def fail(path, *args, **kwargs):
        if str(path) == first.archive_path.name:
            attempts.append(path)
            raise PermissionError("still busy")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail)
        with pytest.raises(PruneError):
            execute_prune_plan(plan, facts)
        assert len(attempts) == 1
        assert {p: p.read_bytes() for p in later.directory.iterdir()} == before
        change(job)
        result = run(job)
        assert result.result == "created" and result.cleanup_errors
        assert len(attempts) == 2  # One attempt in each execution, never a retry loop.
        assert {p: p.read_bytes() for p in later.directory.iterdir()} == before
    change(job)
    assert run(job).result == "created"
    assert not first.directory.exists() and not later.directory.exists()


def test_dry_run_reports_pending_and_orphans_without_mutation(job, monkeypatch, capsys):
    old = run(job).archive_path
    change(job)
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        run(job)
    publish(job, old.parent)
    orphan = old.parent.with_name(old.parent.name + "-3")
    orphan.mkdir()
    before = {
        p: (p.lstat(), p.read_bytes() if p.is_file() else None)
        for p in old.parent.parent.rglob("*")
    }
    assert main(["--user", "--config", str(job), "prune", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"PRUNE_PENDING: {old.parent}" in out and f"EMPTY_ORPHAN: {orphan}" in out
    # Reads can update atime; identity, content, mtime, and directory entries cannot change.
    after = {
        p: (p.lstat(), p.read_bytes() if p.is_file() else None)
        for p in old.parent.parent.rglob("*")
    }
    assert set(before) == set(after)
    for path, (info, content) in before.items():
        assert after[path][0].st_mtime_ns == info.st_mtime_ns
        assert after[path][1] == content


def test_corrupt_survivor_prevents_any_resumed_candidate_modification(job, monkeypatch):
    old = run(job).archive_path
    change(job)
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        survivor = run(job).archive_path
    publish(job, old.parent)
    old.unlink()
    before = {p.name: p.read_bytes() for p in old.parent.iterdir()}
    survivor.write_bytes(b"corrupt content with unchanged sidecar")
    facts = discovery(job)
    plan = build_prune_plan(facts, workflow.load_validated_config(job).retention)
    with pytest.raises(PruneError, match="corrupt"):
        execute_prune_plan(plan, facts)
    assert {p.name: p.read_bytes() for p in old.parent.iterdir()} == before
    assert read_suspect_marker(survivor.parent) is not None


def test_active_per_file_orphan_without_manifest_evidence_can_be_removed(job):
    configure(job, backup_mode="per_file")
    original = run(job).files[0].archive_path
    directory = original.parent
    for path in directory.iterdir():
        path.unlink()
    assert workflow.prune_backups(job, dry_run=False, user_mode=True).removed == 1
    assert not directory.exists()


def test_resumption_requires_flushing_existing_intent_before_any_unlink(job, monkeypatch):
    old = run(job).archive_path
    change(job)
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        run(job)
    publish(job, old.parent)
    before = {p.name: p.read_bytes() for p in old.parent.iterdir()}
    facts = discovery(job)
    plan = build_prune_plan(facts, workflow.load_validated_config(job).retention)
    attempts = []

    def fail(fd):
        attempts.append(fd)
        raise OSError(errno.EIO, "cannot flush intent")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail)
        with pytest.raises(PruneError, match="flush pending deletion intent"):
            execute_prune_plan(plan, facts)
    assert len(attempts) == 1
    assert {p.name: p.read_bytes() for p in old.parent.iterdir()} == before
    assert execute_prune_plan(plan, facts) == (old.parent,)
