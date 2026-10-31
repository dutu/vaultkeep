"""Pending deletion tolerates stale known directory entries without weakening guards."""

from __future__ import annotations

import errno
import os

import pytest

from tests.helpers.backup import job as job
from tests.helpers.backup import run
from vaultkeep.backup import workflow
from vaultkeep.destination import pending as pending_module
from vaultkeep.destination.discovery import discover_backups
from vaultkeep.destination.pending import (
    PRUNE_MARKER_NAME,
    PruneMarker,
    opened_directory,
    publish_marker,
    validate_entries,
)
from vaultkeep.destination.pruning import build_prune_plan, execute_prune_plan
from vaultkeep.destination.suspect import (
    SUSPECT_MARKER_NAME,
    mark_backup_suspect,
    read_suspect_marker,
)
from vaultkeep.errors import PruneError
from vaultkeep.locking import destination_lock


def discover(job):
    return discover_backups(workflow.load_validated_config(job), metadata_only=True)


def stale_listing(monkeypatch, directory, *, ghosts=(), hidden=()):
    original = os.listdir
    identity = directory.stat()

    def listdir(path="."):
        names = original(path)
        if isinstance(path, int):
            current = os.fstat(path)
            if (identity.st_dev, identity.st_ino) == (current.st_dev, current.st_ino):
                return sorted((set(names) | set(ghosts)) - set(hidden))
        return names

    monkeypatch.setattr(os, "listdir", listdir)


@pytest.fixture
def pending_backup(job, monkeypatch):
    archive = run(job).archive_path
    mark_backup_suspect(archive.parent, "original suspect")
    with monkeypatch.context() as patch:
        patch.setattr(workflow, "execute_prune_plan", lambda *args: ())
        survivor = run(job).archive_path
    backup = next(b for b in discover(job).backups if b.directory == archive.parent)
    marker = PruneMarker(
        version=1,
        directory_name=archive.parent.name,
        manifest=backup.manifest,
        suspect=read_suspect_marker(archive.parent),
    )
    with destination_lock(archive.parent.parent), opened_directory(archive.parent) as (parent, fd):
        publish_marker(parent, fd, marker, archive.parent.parent)
    return archive, survivor, marker


@pytest.mark.parametrize("artifact", ["archive", "checksum", "manifest", "suspect", "all"])
def test_pending_discovery_and_resumption_accept_stale_deleted_artifacts(
    job, pending_backup, monkeypatch, artifact
):
    archive, survivor, marker = pending_backup
    names = {
        "archive": archive.name,
        "checksum": archive.name + ".sha256",
        "manifest": marker.base_name + ".json",
        "suspect": SUSPECT_MARKER_NAME,
    }
    removed = marker.artifacts if artifact == "all" else (names[artifact],)
    for name in removed:
        (archive.parent / name).unlink()
    stale_listing(monkeypatch, archive.parent, ghosts=marker.artifacts)
    facts = discover(job)
    assert not facts.malformed and len(facts.pending) == 1
    assert all(b.directory != archive.parent for b in facts.backups)
    plan = build_prune_plan(facts, workflow.load_validated_config(job).retention)
    assert execute_prune_plan(plan, facts) == (archive.parent,)
    assert not archive.parent.exists() and survivor.is_file()


def test_new_backup_cleanup_succeeds_with_stale_names_after_its_own_unlinks(job, monkeypatch):
    archive = run(job).archive_path
    names = tuple(path.name for path in archive.parent.iterdir())
    stale_listing(monkeypatch, archive.parent, ghosts=names)
    (job.parent / "source" / "data").write_bytes(b"changed source")
    result = run(job)
    assert result.result == "created" and result.cleanup_errors == ()
    assert result.removed == 1
    assert not archive.parent.exists() and result.archive_path.is_file()


@pytest.mark.parametrize("metadata", ["manifest", "suspect"])
def test_pending_metadata_disappearing_between_stat_and_open_is_already_deleted(
    job, pending_backup, monkeypatch, metadata
):
    archive, survivor, marker = pending_backup
    name = marker.base_name + ".json" if metadata == "manifest" else SUSPECT_MARKER_NAME
    original = pending_module.read_regular
    attempts = []
    identity = archive.parent.stat()

    def read(descriptor, filename):
        current = os.fstat(descriptor)
        if filename == name and (current.st_dev, current.st_ino) == (
            identity.st_dev,
            identity.st_ino,
        ):
            attempts.append(filename)
            (archive.parent / name).unlink()
            raise FileNotFoundError(errno.ENOENT, "already deleted", name)
        return original(descriptor, filename)

    stale_listing(monkeypatch, archive.parent, ghosts=marker.artifacts)
    monkeypatch.setattr(pending_module, "read_regular", read)
    facts = discover(job)
    assert not facts.malformed and len(facts.pending) == 1
    assert attempts == [name]
    assert workflow.prune_backups(job, dry_run=False, user_mode=True).removed == 1
    assert attempts == [name]
    assert survivor.is_file()


def test_expected_artifact_disappearing_at_unlink_is_not_retried(job, pending_backup, monkeypatch):
    archive, survivor, _ = pending_backup
    original = os.unlink
    attempts = []

    def unlink(path, *args, **kwargs):
        if str(path) == archive.name:
            attempts.append(path)
            original(path, *args, **kwargs)
            raise FileNotFoundError(errno.ENOENT, "another client already deleted", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    assert workflow.prune_backups(job, dry_run=False, user_mode=True).removed == 1
    assert len(attempts) == 1
    assert not archive.parent.exists() and survivor.is_file()


@pytest.mark.parametrize("unsafe", ["unknown-ghost", "hidden-link", "missing-marker", "permission"])
def test_stale_listing_tolerance_does_not_authorize_unsafe_pending_cleanup(
    job, pending_backup, monkeypatch, unsafe
):
    archive, survivor, marker = pending_backup
    if unsafe == "unknown-ghost":
        stale_listing(monkeypatch, archive.parent, ghosts=("unknown-sync-file",))
    elif unsafe == "hidden-link":
        archive.unlink()
        archive.symlink_to(survivor)
        stale_listing(monkeypatch, archive.parent, hidden=(archive.name,))
    elif unsafe == "missing-marker":
        (archive.parent / PRUNE_MARKER_NAME).unlink()
        archive.unlink()
        stale_listing(monkeypatch, archive.parent, ghosts=(*marker.artifacts, PRUNE_MARKER_NAME))
    else:
        original = os.open

        def open_file(path, *args, **kwargs):
            if path == marker.base_name + ".json":
                raise PermissionError(errno.EACCES, "metadata busy", str(path))
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", open_file)
    facts = discover(job)
    assert facts.malformed and not facts.pending
    with pytest.raises(PruneError, match="malformed"):
        workflow.prune_backups(job, dry_run=False, user_mode=True)
    assert archive.parent.is_dir() and survivor.is_file()


def test_unmarked_backup_missing_listed_manifest_is_still_invalid(job, monkeypatch):
    archive = run(job).archive_path
    backup = discover(job).backups[0]
    marker = PruneMarker(version=1, directory_name=archive.parent.name, manifest=backup.manifest)
    manifest_name = marker.base_name + ".json"
    (archive.parent / manifest_name).unlink()
    stale_listing(monkeypatch, archive.parent, ghosts=(manifest_name,))
    with opened_directory(archive.parent) as (_, descriptor), pytest.raises(FileNotFoundError):
        validate_entries(descriptor, marker, pending=False)
    facts = discover(job)
    assert facts.malformed and not facts.backups and not facts.pending


def test_pending_validation_probes_known_artifacts_even_when_listing_omits_them(
    pending_backup, monkeypatch
):
    archive, _, marker = pending_backup
    stale_listing(monkeypatch, archive.parent, hidden=(*marker.artifacts, PRUNE_MARKER_NAME))
    with opened_directory(archive.parent) as (_, descriptor):
        assert validate_entries(descriptor, marker, pending=True) == {
            *marker.artifacts,
            PRUNE_MARKER_NAME,
        }


def stale_positive_attributes(monkeypatch, directory, names):
    """Model cached path attributes surviving successful server-side unlinks."""
    original = os.stat
    identity = directory.stat()
    cached = {name: (directory / name).lstat() for name in names}

    def stat(path, *args, **kwargs):
        descriptor = kwargs.get("dir_fd")
        if descriptor is not None and path in cached:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                return cached[path]
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)


def test_stale_positive_attributes_after_successful_unlinks_do_not_stop_cleanup(job, monkeypatch):
    archive = run(job).archive_path
    names = tuple(path.name for path in archive.parent.iterdir())
    stale_listing(monkeypatch, archive.parent, ghosts=names)
    stale_positive_attributes(monkeypatch, archive.parent, names)
    original = os.unlink
    attempts = []

    def unlink(path, *args, **kwargs):
        if path in names:
            attempts.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    (job.parent / "source" / "data").write_bytes(b"new version")
    result = run(job)
    assert result.result == "created" and result.cleanup_errors == ()
    assert result.removed == 1
    assert sorted(attempts) == sorted(names)
    assert not archive.parent.exists() and result.archive_path.is_file()


def test_stale_positive_attributes_do_not_strand_marker_only_pending_directory(
    job, pending_backup, monkeypatch
):
    archive, survivor, marker = pending_backup
    stale_listing(monkeypatch, archive.parent, ghosts=marker.artifacts)
    stale_positive_attributes(monkeypatch, archive.parent, marker.artifacts)
    for name in marker.artifacts:
        (archive.parent / name).unlink()
    facts = discover(job)
    assert not facts.malformed and len(facts.pending) == 1
    original = os.unlink
    attempts = []

    def unlink(path, *args, **kwargs):
        if path in marker.artifacts:
            attempts.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    assert workflow.prune_backups(job, dry_run=False, user_mode=True).removed == 1
    assert attempts == []  # Confirmed-missing artifacts are never unlinked again.
    assert not archive.parent.exists() and survivor.is_file()


@pytest.mark.parametrize("restored", ["archive", "checksum", "unexpected", "symlink"])
def test_actual_reappearing_artifacts_preserve_intent_and_stop_without_retry(
    job, pending_backup, monkeypatch, restored
):
    archive, survivor, marker = pending_backup
    stale_positive_attributes(monkeypatch, archive.parent, marker.artifacts)
    original = os.unlink
    attempts = []
    restored_path = None

    def unlink(path, *args, **kwargs):
        nonlocal restored_path
        if path in marker.artifacts:
            attempts.append(path)
        original(path, *args, **kwargs)
        if path == marker.artifacts[-1]:
            name = {
                "archive": archive.name,
                "checksum": archive.name + ".sha256",
                "unexpected": "foreign-sync-file",
                "symlink": archive.name,
            }[restored]
            restored_path = archive.parent / name
            if restored == "symlink":
                restored_path.symlink_to(survivor)
            else:
                restored_path.write_bytes(b"reappeared after successful unlink")

    monkeypatch.setattr(os, "unlink", unlink)
    with pytest.raises(PruneError):
        workflow.prune_backups(job, dry_run=False, user_mode=True)
    assert attempts == list(marker.artifacts)
    assert (archive.parent / PRUNE_MARKER_NAME).is_file()
    assert restored_path is not None and restored_path.exists()
    if restored != "symlink":
        assert restored_path.read_bytes() == b"reappeared after successful unlink"
    assert survivor.is_file()
