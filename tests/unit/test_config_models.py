"""Tests for strict v1 configuration models."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from vaultkeep.config import JobConfig

EXAMPLE_CONFIG = Path(__file__).parents[2] / "examples" / "vaultkeep-job.yaml.disabled"


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


def test_complete_configuration_is_accepted(valid_config: dict[str, Any]) -> None:
    config = JobConfig.model_validate(valid_config)

    assert config.config_version == 1
    assert config.job.id == "app"
    assert config.archive.compression_level == 6


def test_disabled_example_matches_strict_models() -> None:
    yaml = YAML(typ="safe", pure=True)
    yaml.version = (1, 2)
    yaml.allow_duplicate_keys = False

    config = JobConfig.model_validate(yaml.load(EXAMPLE_CONFIG))

    assert config.job.id == "example"
    assert config.schedule.enabled is False


def test_unknown_property_is_rejected(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["logging"]["levle"] = "debug"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        JobConfig.model_validate(candidate)


@pytest.mark.parametrize("invalid_value", ["yes", 1, 0])
def test_boolean_coercion_is_rejected(valid_config: dict[str, Any], invalid_value: object) -> None:
    candidate = deepcopy(valid_config)
    candidate["schedule"]["enabled"] = invalid_value

    with pytest.raises(ValidationError, match="valid boolean"):
        JobConfig.model_validate(candidate)


@pytest.mark.parametrize("invalid_value", ["24", -1, 2.5, True])
def test_retention_requires_non_negative_strict_integers(
    valid_config: dict[str, Any], invalid_value: object
) -> None:
    candidate = deepcopy(valid_config)
    candidate["retention"]["hourly"] = invalid_value

    with pytest.raises(ValidationError):
        JobConfig.model_validate(candidate)


def test_unsupported_config_version_is_rejected(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["config_version"] = 2

    with pytest.raises(ValidationError, match="Input should be 1"):
        JobConfig.model_validate(candidate)


def test_at_least_one_source_is_required(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["sources"] = []

    with pytest.raises(ValidationError, match="at least 1 item"):
        JobConfig.model_validate(candidate)


def test_hook_timeout_bounds_are_enforced(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["hooks"]["before_check"] = {
        "command": ["/usr/local/sbin/prepare-app-backup"],
        "timeout_seconds": 3601,
    }

    with pytest.raises(ValidationError, match="less than or equal to 3600"):
        JobConfig.model_validate(candidate)


def test_models_are_immutable(valid_config: dict[str, Any]) -> None:
    config = JobConfig.model_validate(valid_config)

    with pytest.raises(ValidationError, match="Instance is frozen"):
        config.job.id = "changed"
