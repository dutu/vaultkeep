from __future__ import annotations

import os
from types import SimpleNamespace

import psutil

from vaultkeep.application import PsutilProcessProvider


class _Process:
    def __init__(self, pid: int, uid: int, *, cmdline_readable: bool = True) -> None:
        self.pid = pid
        self.info: dict[str, object] = {"pid": pid, "uids": SimpleNamespace(real=uid)}
        self.cmdline_readable = cmdline_readable

    @staticmethod
    def create_time() -> float:
        return 1.0

    @staticmethod
    def exe() -> str:
        return "/opt/app/binary"

    def cmdline(self) -> list[str]:
        if not self.cmdline_readable:
            raise psutil.AccessDenied(self.pid)
        return ["/opt/app/binary"]


def test_snapshot_excludes_other_users_before_metadata_reads(monkeypatch) -> None:
    mine = _Process(42, os.geteuid())
    other = _Process(99, os.geteuid() + 1)

    def unexpected_read():
        raise AssertionError("other user's metadata inspected")

    monkeypatch.setattr(other, "cmdline", unexpected_read)
    monkeypatch.setattr("vaultkeep.application.psutil.process_iter", lambda _attrs: [mine, other])
    monkeypatch.setattr(PsutilProcessProvider, "alive", lambda *args: True)
    snapshot = PsutilProcessProvider().snapshot()
    assert [proc.pid for proc in snapshot] == [42]


def test_unreadable_command_line_is_retained_as_uncertainty(monkeypatch) -> None:
    mine = _Process(42, os.geteuid(), cmdline_readable=False)
    monkeypatch.setattr("vaultkeep.application.psutil.process_iter", lambda _attrs: [mine])
    monkeypatch.setattr(PsutilProcessProvider, "alive", lambda *args: True)
    snapshot = PsutilProcessProvider().snapshot()
    assert len(snapshot) == 1 and not snapshot[0].cmdline_available


def test_unknown_process_ownership_blocks_snapshot(monkeypatch) -> None:
    mine = _Process(42, os.geteuid())
    mine.info["uids"] = None
    monkeypatch.setattr("vaultkeep.application.psutil.process_iter", lambda _attrs: [mine])
    import pytest

    with pytest.raises(psutil.AccessDenied):
        PsutilProcessProvider().snapshot()


def test_root_provider_inspects_configured_owner_instead_of_executor(monkeypatch):
    target = _Process(42, 4242)
    executor = _Process(99, 0)
    monkeypatch.setattr("vaultkeep.application.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "vaultkeep.application.psutil.process_iter", lambda _attrs: [executor, target]
    )
    monkeypatch.setattr(PsutilProcessProvider, "alive", lambda *args: True)
    snapshot = PsutilProcessProvider(owner_uid=4242).snapshot()
    assert [proc.pid for proc in snapshot] == [42]
