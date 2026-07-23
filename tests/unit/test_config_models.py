"""Tests for strict v1 configuration models."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from vaultkeep.config import JobConfig, load_config

EXAMPLE_CONFIG = Path(__file__).parents[2] / "examples" / "vaultkeep-job.yaml.disabled"


def test_complete_configuration_is_accepted(valid_config: dict[str, Any]) -> None:
    config = JobConfig.model_validate(valid_config)

    assert config.config_version == 1
    assert config.job.id == "app"
    assert config.archive.compression_level == 6


def test_disabled_example_matches_strict_models() -> None:
    config = load_config(EXAMPLE_CONFIG)

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


def test_documented_defaults_are_applied(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["source_options"] = {}
    candidate["schedule"].pop("persistent")

    config = JobConfig.model_validate(candidate)

    assert config.source_options.follow_symlinks is False
    assert config.source_options.cross_filesystems is False
    assert config.source_options.ignore_missing is False
    assert config.schedule.persistent is True
