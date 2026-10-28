"""Shared test fixtures."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
from packaging.tags import sys_tags
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename


@pytest.fixture
def system_execution(monkeypatch):
    """Authorize disposable system-scope unit tests without changing filesystem UIDs."""
    from vaultkeep import application, schedule, validation
    from vaultkeep.backup import workflow

    uid = os.geteuid()

    def owner(user_mode, *, config_path=None):
        return (uid, None) if user_mode else (0, 0)

    for module in (application, schedule, validation, workflow):
        monkeypatch.setattr(module, "runtime_owner", owner)


@pytest.fixture(scope="session")
def installer_wheelhouse() -> Path:
    """Skip wheel-installing tests unless the locked wheels support this interpreter."""
    wheelhouse = Path(
        os.environ.get("VAULTKEEP_TEST_WHEELHOUSE", "/tmp/vaultkeep-wheelhouse")
    ).resolve()
    supported_tags = set(sys_tags())
    available = set()
    for wheel in wheelhouse.glob("*.whl"):
        if not wheel.is_file():
            continue
        try:
            name, version, _, tags = parse_wheel_filename(wheel.name)
        except InvalidWheelFilename:
            continue
        if tags & supported_tags:
            available.add((name, str(version)))
    lock = Path(__file__).resolve().parents[1] / "requirements.lock"
    required = re.findall(r"^([\w-]+)==([^\s\\]+)", lock.read_text(), re.MULTILINE)
    missing = [
        f"{name}=={version}"
        for name, version in required
        if (canonicalize_name(name), version) not in available
    ]
    if missing:
        pytest.skip(
            f"Offline installer wheelhouse {wheelhouse} lacks compatible locked wheels: "
            f"{', '.join(missing)}; prepare it using docs/development/testing.md"
        )
    return wheelhouse


@pytest.fixture
def valid_config() -> dict[str, Any]:
    """Return a complete valid schema-v2 configuration mapping."""
    return {
        "config_version": 2,
        "job": {"id": "app"},
        "sources": [
            {
                "path": "/srv/app",
                "archive_path_mode": "prefix",
                "archive_prefix": "app",
                "exclude": ["cache/"],
            }
        ],
        "exclude": ["*.tmp"],
        "source_options": {
            "follow_symlinks": False,
            "cross_filesystems": False,
            "ignore_missing": False,
        },
        "destination": {
            "root": "/mnt/backups/app",
            "name_template": "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}",
        },
        "archive": {"format": "tar.zst", "compression_level": 6},
        "encryption": {"mode": "none"},
        "retention": {
            "hourly": 24,
            "daily": 7,
            "weekly": 8,
            "monthly": 12,
            "yearly": 3,
        },
        "schedule": {
            "enabled": False,
            "interval": "daily",
            "window": "01:00-05:00",
            "persistent": True,
        },
        "hooks": {
            "before_check": None,
            "before_archive": None,
            "after_archive": None,
            "on_success": None,
            "on_failure": None,
            "on_unchanged": None,
        },
        "logging": {"level": "info", "include_command_output": False},
    }
