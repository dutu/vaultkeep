from __future__ import annotations

import os
from pathlib import Path

from vaultkeep_trigger.environment import build_child_environment
from vaultkeep_trigger.paths import (
    account_home,
    expand_user_path,
    state_root,
)


def test_state_root_is_fixed(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/tmp/decoy-home")
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/decoy-state")
    expected = account_home() / ".local" / "state" / "vaultkeep-trigger"
    assert state_root() == expected


def test_expand_user_uses_account_home() -> None:
    assert expand_user_path("~/x") == str(account_home() / "x")
    assert expand_user_path("~") == str(account_home())
    assert expand_user_path("/abs") == "/abs"


def test_child_environment(tmp_path: Path) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    env = build_child_environment(
        home=Path("/home/user"),
        runtime_dir=runtime,
        environ={
            "XDG_STATE_HOME": "/home/user/.local/state",
            "XDG_CACHE_HOME": "relative-ignored",
            "PATH": "/evil",
        },
    )
    assert env["HOME"] == "/home/user"
    assert env["XDG_RUNTIME_DIR"] == str(runtime)
    assert env["XDG_STATE_HOME"] == "/home/user/.local/state"
    assert env["XDG_CACHE_HOME"] == "/home/user/.cache"
    assert env["PATH"] != "/evil"


def test_runtime_dir_validation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    from vaultkeep_trigger.paths import runtime_dir

    assert runtime_dir() == tmp_path
    monkeypatch.setenv("XDG_RUNTIME_DIR", "not-absolute")
    assert runtime_dir() is None
    assert os.geteuid() == Path(tmp_path).stat().st_uid
