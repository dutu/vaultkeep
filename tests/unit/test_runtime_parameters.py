"""Tests for explicit runtime template parameters."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from vaultkeep.config import JobConfig, parse_runtime_parameters, resolve_runtime_config
from vaultkeep.errors import ConfigurationError
from vaultkeep.sources.hashing import calculate_config_fingerprint
from vaultkeep.state.identity import job_identity_hash


def test_runtime_parameters_render_destination_root_and_require_usage(
    valid_config: dict[str, Any],
) -> None:
    candidate = deepcopy(valid_config)
    candidate["destination"]["root"] = "/mnt/backups/{repo}"
    candidate["destination"]["name_template"] = "backup-{job}-{repo}-{timestamp_utc:%Y%m%dT%H%M%SZ}"

    config = resolve_runtime_config(JobConfig.model_validate(candidate), {"repo": "nas-a"})

    assert config.destination.root == "/mnt/backups/nas-a"


def test_missing_runtime_parameter_is_reported(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["destination"]["root"] = "/mnt/backups/{repo}"

    with pytest.raises(ConfigurationError) as captured:
        resolve_runtime_config(JobConfig.model_validate(candidate), {})

    assert {issue.code for issue in captured.value.issues} == {"param_missing"}


def test_unused_runtime_parameter_is_rejected(valid_config: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationError) as captured:
        resolve_runtime_config(JobConfig.model_validate(valid_config), {"repo": "nas-a"})

    assert {issue.code for issue in captured.value.issues} == {"param_unused"}


def test_runtime_parameter_parser_rejects_reserved_names() -> None:
    with pytest.raises(ConfigurationError) as captured:
        parse_runtime_parameters(["timestamp_utc=value"])

    assert {issue.code for issue in captured.value.issues} == {"param_reserved"}


def test_runtime_parameters_affect_fingerprint_and_identity(
    tmp_path: Any, valid_config: dict[str, Any]
) -> None:
    config_path = tmp_path / "app.yaml"
    config_path.write_text("", encoding="utf-8")
    config = JobConfig.model_validate(valid_config)

    assert calculate_config_fingerprint(config, runtime_params={"repo": "a"}) != (
        calculate_config_fingerprint(config, runtime_params={"repo": "b"})
    )
    assert job_identity_hash(config_path, "app", runtime_params={"repo": "a"}) != (
        job_identity_hash(config_path, "app", runtime_params={"repo": "b"})
    )
