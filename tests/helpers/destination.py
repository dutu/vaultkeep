"""Disposable destination builders and emulated network mounts."""

from __future__ import annotations

import ctypes
import errno
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaultkeep import locking
from vaultkeep.destination import finalize as finalize_module


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


def _staging(tmp_path: Path) -> Path:
    staging = tmp_path / "~$.partial-vaultkeep-app-abc"
    staging.mkdir()
    (staging / "archive.tar.zst").write_bytes(b"archive")
    (staging / "archive.tar.zst.sha256").write_text("checksum", encoding="utf-8")
    (staging / "backup.json").write_text("{}", encoding="utf-8")
    return staging
