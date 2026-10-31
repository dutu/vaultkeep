"""Shared pytest fixtures for the Trigger test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.trigger import RecordingNotifier
from vaultkeep.trigger.state import StateStore


@pytest.fixture(autouse=True)
def isolated_monitor_discovery(tmp_path, monkeypatch):
    """Trigger unit tests never inspect the account's real systemd services."""
    monkeypatch.setattr("vaultkeep.trigger.service.units_root", lambda: tmp_path / "units")
    monkeypatch.setattr("vaultkeep.trigger.service._systemctl", lambda *_: "")


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    result = StateStore(tmp_path / "state")
    result.ensure()
    return result


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()
