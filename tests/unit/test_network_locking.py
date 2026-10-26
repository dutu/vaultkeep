"""Network mount policy with real file-lock contention on disposable local files."""

from __future__ import annotations

import ctypes
import errno
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_destination_finalize import _staging
from vaultkeep import locking
from vaultkeep.destination import finalize as finalize_module
from vaultkeep.errors import DestinationError, LockError


@pytest.fixture
def share(tmp_path, monkeypatch):
    root = tmp_path / "share with spaces"
    root.mkdir()
    settings = SimpleNamespace(root=root, filesystem="cifs", options="rw", mount_root="/")
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == Path("/proc/self/mountinfo"):
            escaped = str(root).replace(" ", r"\040")
            return (
                "1 0 1:1 / / rw - tmpfs tmpfs rw\n"
                f"2 1 2:1 {settings.mount_root} {escaped} rw - "
                f"{settings.filesystem} server:/share {settings.options}\n"
            )
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(locking, "_destination_mount_id", lambda path: 2)
    return settings


@pytest.mark.parametrize("filesystem", ["cifs", "smb3", "nfs", "nfs4"])
def test_network_lock_covers_share_aliases_siblings_and_parent_child(share, filesystem):
    share.filesystem = filesystem
    first, second = share.root / "first", share.root / "second"
    first.mkdir()
    second.mkdir()
    child = first / "child"
    child.mkdir()
    alias = share.root.parent / "alias"
    alias.symlink_to(first, target_is_directory=True)
    lock = share.root / locking.NETWORK_LOCK_NAME

    with locking.destination_lock(first):
        assert lock.is_file()
        assert locking.held_destination_filesystem(child) == filesystem
        assert locking.held_destination_filesystem(second) == filesystem
        assert locking.held_destination_filesystem(share.root.parent) is None
        for other in (first, second, child, alias, share.root):
            with pytest.raises(LockError), locking.destination_lock(other):
                pytest.fail("overlapping share writer acquired a second lock")
    assert locking.held_destination_filesystem(first) is None
    before = lock.stat()
    with locking.destination_lock(alias):
        assert lock.stat().st_ino == before.st_ino
    assert lock.stat().st_ino == before.st_ino and lock.stat().st_size == 0
    assert not list(first.glob(".vaultkeep*"))


@pytest.mark.parametrize("filesystem", ["cifs", "nfs4"])
def test_share_lock_excludes_separate_process_and_releases_after_error(share, filesystem):
    share.filesystem = filesystem
    lock = share.root / locking.NETWORK_LOCK_NAME
    script = (
        "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR); "
        "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
    )
    with (
        pytest.raises(RuntimeError, match="execution failed"),
        locking.destination_lock(share.root),
    ):
        child = subprocess.run(
            [sys.executable, "-c", script, str(lock)], capture_output=True, timeout=10
        )
        assert child.returncode != 0 and b"BlockingIOError" in child.stderr
        raise RuntimeError("execution failed")
    child = subprocess.run(
        [sys.executable, "-c", script, str(lock)], capture_output=True, timeout=10
    )
    assert child.returncode == 0, child.stderr


@pytest.mark.parametrize(
    "filesystem,options",
    [
        ("cifs", "rw,nobrl"),
        ("smb3", "rw,nobrl"),
        ("nfs", "rw,nolock"),
        ("nfs4", "rw,local_lock=all"),
        ("nfs", "rw,local_lock=flock"),
    ],
)
def test_client_local_locks_are_rejected_before_creating_anchor(share, filesystem, options):
    share.filesystem, share.options = filesystem, options
    with (
        pytest.raises(DestinationError, match="disables cross-machine locking"),
        locking.destination_lock(share.root),
    ):
        pytest.fail("unsafe network mount was accepted")
    assert list(share.root.iterdir()) == []


@pytest.mark.parametrize("options", ["rw,local_lock=none", "rw,local_lock=posix", "rw,lock"])
def test_nfs_server_flock_settings_are_accepted(share, options):
    share.filesystem, share.options = "nfs", options
    with locking.destination_lock(share.root):
        pass


@pytest.mark.parametrize("filesystem", ["cifs", "smb3", "nfs4"])
def test_smb_remote_lock_denial_is_distinct_from_anchor_open_failure(
    share, monkeypatch, filesystem
):
    import fcntl

    share.filesystem = filesystem

    def denied(*args):
        raise PermissionError(errno.EACCES, "server lock denied")

    monkeypatch.setattr(fcntl, "flock", denied)
    expected = LockError if filesystem in {"cifs", "smb3"} else DestinationError
    with pytest.raises(expected), locking.destination_lock(share.root):
        pytest.fail("denied server lock was ignored")
    assert locking.held_destination_filesystem(share.root) is None


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "hardlink"])
def test_network_anchor_must_be_a_single_regular_file(share, kind):
    lock = share.root / locking.NETWORK_LOCK_NAME
    target = share.root / "operator-file"
    target.write_bytes(b"preserved")
    if kind == "symlink":
        lock.symlink_to(target)
    elif kind == "directory":
        lock.mkdir()
    elif kind == "fifo":
        os.mkfifo(lock)
    else:
        os.link(target, lock)
    with pytest.raises(DestinationError), locking.destination_lock(share.root):
        pytest.fail("unsafe shared anchor was accepted")
    assert target.read_bytes() == b"preserved"


@pytest.mark.parametrize("failure", ["permission", "unsupported", "read-only", "subtree"])
def test_network_locking_failure_never_falls_back_to_a_local_lock(share, monkeypatch, failure):
    if failure == "read-only":
        share.options = "ro"
    elif failure == "subtree":
        share.mount_root = "/hidden/subtree"
    elif failure == "unsupported":
        import fcntl

        def unsupported(*args):
            raise OSError(errno.ENOLCK, "server locks unavailable")

        monkeypatch.setattr(fcntl, "flock", unsupported)
    else:
        original = os.open

        def denied(path, *args, **kwargs):
            if Path(path).name == locking.NETWORK_LOCK_NAME:
                raise PermissionError("anchor inaccessible")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", denied)
    with pytest.raises(DestinationError), locking.destination_lock(share.root):
        pytest.fail("network locking failure was ignored")
    assert locking.held_destination_filesystem(share.root) is None


def test_replaced_lock_anchor_is_rejected_before_history_access(share, monkeypatch):
    import fcntl

    flock = fcntl.flock
    lock = share.root / locking.NETWORK_LOCK_NAME

    def replaced(fd, operation):
        flock(fd, operation)
        lock.rename(lock.with_name("operator-moved-lock"))
        lock.touch()

    monkeypatch.setattr(fcntl, "flock", replaced)
    with (
        pytest.raises(DestinationError, match="changed during acquisition"),
        locking.destination_lock(share.root),
    ):
        pytest.fail("replaced anchor was accepted")


def test_visible_mount_id_selects_stacked_mount_independently_of_record_order(share, monkeypatch):
    original = Path.read_text

    def stacked(path, *args, **kwargs):
        text = original(path, *args, **kwargs)
        if path == Path("/proc/self/mountinfo"):
            # The visible record appears before the hidden mount record.
            lines = text.splitlines()
            visible = lines[1].replace("2 1 2:1", "3 2 3:1").replace("cifs", "nfs4")
            return "\n".join((lines[0], visible, lines[1])) + "\n"
        return text

    monkeypatch.setattr(Path, "read_text", stacked)
    monkeypatch.setattr(locking, "_destination_mount_id", lambda path: 3)
    with locking.destination_lock(share.root):
        assert locking.held_destination_filesystem(share.root) == "nfs4"


@pytest.fixture
def no_nfs_noreplace(monkeypatch):
    return reject_native_noreplace(monkeypatch)


def reject_native_noreplace(monkeypatch):
    native = SimpleNamespace(error=errno.EINVAL, calls=0)

    def renameat2(*args):
        native.calls += 1
        ctypes.set_errno(native.error)
        return -1

    monkeypatch.setattr(
        finalize_module.ctypes, "CDLL", lambda *args, **kw: SimpleNamespace(renameat2=renameat2)
    )
    return native


@pytest.mark.parametrize("filesystem", ["nfs", "nfs4"])
@pytest.mark.parametrize("native_error", [errno.EINVAL, errno.EOPNOTSUPP])
def test_nfs_probe_and_commit_fallback_hold_shared_lock(
    share, no_nfs_noreplace, monkeypatch, filesystem, native_error
):
    share.filesystem = filesystem
    no_nfs_noreplace.error = native_error
    rename = os.rename
    calls = []

    def checked(source, destination):
        assert locking.held_destination_filesystem(source.parent) == filesystem
        calls.append((source, destination))
        rename(source, destination)

    monkeypatch.setattr(finalize_module.os, "rename", checked)
    finalize_module.probe_destination_writability(share.root)
    staging = _staging(share.root)
    final = share.root / "backup"
    with locking.destination_lock(share.root):
        finalize_module.finalize_backup_directory(staging, final)
    assert (final / "archive.tar.zst").read_bytes() == b"archive"
    assert not staging.exists() and len(calls) == no_nfs_noreplace.calls == 2
    assert set(share.root.iterdir()) == {final, share.root / locking.NETWORK_LOCK_NAME}


@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("filesystem", ["nfs4", "cifs", "tmpfs"])
def test_rename_fallback_requires_covering_nfs_lock(share, no_nfs_noreplace, held, filesystem):
    from contextlib import nullcontext

    share.filesystem = filesystem
    staging = _staging(share.root)
    final = share.root / "backup"
    with nullcontext() if not held else locking.destination_lock(share.root):
        if held and filesystem == "nfs4":
            finalize_module._rename_noreplace(staging, final)
            assert final.is_dir() and not staging.exists()
        else:
            with pytest.raises(OSError, match="Invalid argument"):
                finalize_module._rename_noreplace(staging, final)
            assert staging.is_dir() and not final.exists()


@pytest.mark.parametrize("target", ["empty", "nonempty", "symlink"])
def test_nfs_fallback_preserves_existing_target(share, no_nfs_noreplace, target):
    share.filesystem = "nfs"
    staging = _staging(share.root)
    final = share.root / "backup"
    if target == "symlink":
        final.symlink_to(share.root / "missing")
    else:
        final.mkdir()
        if target == "nonempty":
            (final / "data").write_bytes(b"operator data")
    with locking.destination_lock(share.root), pytest.raises(FileExistsError):
        finalize_module._rename_noreplace(staging, final)
    assert staging.is_dir()
    if target == "nonempty":
        assert (final / "data").read_bytes() == b"operator data"
    elif target == "symlink":
        assert final.is_symlink()
    else:
        assert list(final.iterdir()) == []


@pytest.mark.parametrize("error", [errno.EACCES, errno.EIO, errno.ENOSYS])
def test_nfs_fallback_does_not_hide_other_rename_failures(share, no_nfs_noreplace, error):
    share.filesystem = "nfs4"
    no_nfs_noreplace.error = error
    staging = _staging(share.root)
    final = share.root / "backup"
    with locking.destination_lock(share.root), pytest.raises(OSError) as caught:
        finalize_module._rename_noreplace(staging, final)
    assert caught.value.errno == error and staging.is_dir() and not final.exists()


def test_nfs_fallback_cannot_overwrite_nonempty_target_created_during_rename(
    share, no_nfs_noreplace, monkeypatch
):
    share.filesystem = "nfs4"
    staging = _staging(share.root)
    final = share.root / "backup"
    rename = os.rename

    def competing_writer(source, destination):
        destination.mkdir()
        (destination / "data").write_bytes(b"preserve racing writer")
        rename(source, destination)

    monkeypatch.setattr(finalize_module.os, "rename", competing_writer)
    with locking.destination_lock(share.root), pytest.raises(OSError):
        finalize_module._rename_noreplace(staging, final)
    assert (final / "data").read_bytes() == b"preserve racing writer" and staging.is_dir()
