"""CIFS marker preparation uses share access controls rather than synthetic modes."""

from __future__ import annotations

import errno
import os
import stat

import pytest

from tests.helpers.backup import configure, run
from tests.helpers.backup import job as job
from tests.helpers.destination import share as share
from vaultkeep.backup import workflow
from vaultkeep.destination.discovery import discover_backups
from vaultkeep.destination.pending import (
    PRUNE_MARKER_NAME,
    TEMP_DIRECTORY_NAME,
    PruneMarker,
    opened_directory,
    publish_marker,
    read_marker,
)
from vaultkeep.errors import PruneError
from vaultkeep.locking import destination_lock


def prepare(job, share):
    config = workflow.load_validated_config(job)
    config = configure(job, destination=config.destination.model_dump() | {"root": str(share.root)})
    archive = run(job).archive_path
    backup = discover_backups(config, metadata_only=True).backups[0]
    marker = PruneMarker(version=1, directory_name=backup.directory.name, manifest=backup.manifest)
    temporary = share.root / TEMP_DIRECTORY_NAME
    temporary.mkdir(mode=0o700)
    return archive, marker, temporary


def mapped_directory_stats(
    monkeypatch, directory, *, foreign_owner=True, mode=0o770, other_device=False
):
    original = os.fstat
    expected = directory.stat()

    def fstat(fd):
        info = original(fd)
        if (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino):
            values = list(info)
            values[0] = stat.S_IFDIR | mode
            values[2] += int(other_device)
            values[4] = os.geteuid() + 100000 if foreign_owner else os.geteuid()
            values[5] = os.getegid() + 100000
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "fstat", fstat)


@pytest.mark.parametrize("filesystem", ["cifs", "smb3", "ext4", "tmpfs", "nfs", "nfs4"])
@pytest.mark.parametrize("foreign_owner,mode", [(True, 0o700), (False, 0o770), (True, 0o770)])
def test_mapped_ownership_and_modes_are_accepted_only_on_locked_smb(
    job, share, monkeypatch, filesystem, foreign_owner, mode
):
    share.filesystem = filesystem
    archive, marker, temporary = prepare(job, share)
    mapped_directory_stats(monkeypatch, temporary, foreign_owner=foreign_owner, mode=mode)
    before = {p.name: p.read_bytes() for p in archive.parent.iterdir()}
    with destination_lock(share.root), opened_directory(archive.parent) as (parent, descriptor):
        if filesystem in {"cifs", "smb3"}:
            publish_marker(parent, descriptor, marker, share.root)
            assert read_marker(descriptor) == marker
        else:
            with pytest.raises(PruneError, match="private and owned"):
                publish_marker(parent, descriptor, marker, share.root)
            assert {p.name: p.read_bytes() for p in archive.parent.iterdir()} == before
    assert archive.is_file()


@pytest.mark.parametrize("filesystem", ["cifs", "smb3"])
def test_cifs_cleanup_can_defer_and_resume_with_mount_defined_permissions(
    job, share, monkeypatch, filesystem
):
    share.filesystem = filesystem
    archive, _, temporary = prepare(job, share)
    temporary.chmod(0o770)
    mapped_directory_stats(monkeypatch, temporary)
    original_unlink = os.unlink
    attempts = []

    def deny_archive(path, *args, **kwargs):
        if str(path) == archive.name:
            attempts.append(path)
            raise PermissionError(errno.EACCES, "synchronization busy", str(path))
        return original_unlink(path, *args, **kwargs)

    source = job.parent / "source" / "data"
    source.write_bytes(b"second version")
    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", deny_archive)
        result = run(job)
    assert result.result == "created" and result.cleanup_errors
    assert len(attempts) == 1
    assert (archive.parent / PRUNE_MARKER_NAME).is_file()
    config = workflow.load_validated_config(job)
    assert len(discover_backups(config, metadata_only=True).pending) == 1
    source.write_bytes(b"third version")
    result = run(job)
    assert result.result == "created" and not result.cleanup_errors
    assert not archive.parent.exists()
    assert not discover_backups(config, metadata_only=True).pending
    assert workflow.verify_backups(job, user_mode=True).backups == 1
    assert temporary.stat().st_mode & 0o777 == 0o770


@pytest.mark.parametrize("filesystem", ["ext4", "cifs", "smb3"])
def test_marker_publication_requires_exact_destination_lock(job, share, filesystem):
    share.filesystem = filesystem
    archive, marker, _ = prepare(job, share)
    with (
        opened_directory(archive.parent) as (parent, descriptor),
        pytest.raises(PruneError, match="Destination lock required"),
    ):
        publish_marker(parent, descriptor, marker, share.root)
    other = share.root / "other-history"
    other.mkdir()
    with (
        destination_lock(other),
        opened_directory(archive.parent) as (parent, descriptor),
        pytest.raises(PruneError, match="Destination lock required"),
    ):
        publish_marker(parent, descriptor, marker, share.root)
    assert not (archive.parent / PRUNE_MARKER_NAME).exists()


@pytest.mark.parametrize("unsafe", ["symlink", "other-device", "wrong-parent"])
def test_smb_exception_preserves_directory_and_filesystem_guards(job, share, monkeypatch, unsafe):
    archive, marker, temporary = prepare(job, share)
    if unsafe == "symlink":
        temporary.rmdir()
        outside = job.parent / "outside"
        outside.mkdir()
        temporary.symlink_to(outside, target_is_directory=True)
    elif unsafe == "other-device":
        mapped_directory_stats(monkeypatch, temporary, other_device=True)
    with (
        destination_lock(share.root),
        opened_directory(archive.parent) as (parent, descriptor),
        pytest.raises((PruneError, OSError)),
    ):
        # A lock for the requested path cannot authorize a different parent fd.
        publish_marker(
            descriptor if unsafe == "wrong-parent" else parent, descriptor, marker, share.root
        )
    assert archive.is_file() and not (archive.parent / PRUNE_MARKER_NAME).exists()
