"""Shared test fixtures."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def valid_config() -> dict[str, Any]:
    """Return a complete valid schema-v1 configuration mapping."""
    return {
        "config_version": 1,
        "job": {"id": "app"},
        "sources": [{"path": "/srv/app", "exclude": ["cache/"]}],
        "exclude": ["*.tmp"],
        "source_options": {
            "follow_symlinks": False,
            "cross_filesystems": False,
            "ignore_missing": False,
        },
        "destination": {
            "root": "/mnt/backups/app",
            "name_template": "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}",
            "require_mount": True,
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
