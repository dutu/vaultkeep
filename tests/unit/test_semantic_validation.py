"""Tests for schema-v1 cross-field validation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from vaultkeep.config import JobConfig
from vaultkeep.errors import ConfigurationError
from vaultkeep.validation import validate_semantics


def _config(candidate: dict[str, Any]) -> JobConfig:
    return JobConfig.model_validate(candidate)


def _issue_codes(config: JobConfig, *, path: Path | None = None) -> set[str]:
    with pytest.raises(ConfigurationError) as captured:
        validate_semantics(config, config_path=path)
    return {issue.code for issue in captured.value.issues}


def test_valid_default_configuration_passes(valid_config: dict[str, Any]) -> None:
    validate_semantics(_config(valid_config))


def test_semantics_collect_path_and_overlap_errors(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["sources"] = [
        {"path": "relative"},
        {"path": "/srv/app"},
        {"path": "/srv/app/config"},
    ]
    candidate["destination"]["root"] = "/srv/app/backups"

    codes = _issue_codes(_config(candidate))

    assert {"path_absolute", "source_overlap", "destination_source_overlap"} <= codes


def test_duplicate_normalized_sources_are_rejected(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["sources"] = [{"path": "/srv/app"}, {"path": "/srv/other/../app"}]

    assert "source_duplicate" in _issue_codes(_config(candidate))


@pytest.mark.parametrize(
    "marker", ["/absolute/marker", "../outside", "nested/../../outside", "a\0b"]
)
def test_marker_file_must_remain_below_destination(
    valid_config: dict[str, Any], marker: str
) -> None:
    candidate = deepcopy(valid_config)
    candidate["destination"]["marker_file"] = marker

    assert "marker_path" in _issue_codes(_config(candidate))


@pytest.mark.parametrize(
    ("template", "expected_code"),
    [
        ("backup-{job}", "template_timestamp"),
        ("backup-{date}-{timestamp_utc}", "template_placeholder"),
        ("backup-{backup_id}-{timestamp_utc}", "template_backup_id"),
        ("backup/{timestamp_utc}", "template_separator"),
        ("backup-..-{timestamp_utc}", "template_parent"),
        ("backup-{job!r}-{timestamp_utc}", "template_conversion"),
        ("backup-{job:>10}-{timestamp_utc}", "template_format"),
        ("backup-{source_hash:.x}-{timestamp_utc}", "template_format"),
        ("backup-{timestamp_utc", "template_syntax"),
    ],
)
def test_invalid_naming_templates_are_rejected(
    valid_config: dict[str, Any], template: str, expected_code: str
) -> None:
    candidate = deepcopy(valid_config)
    candidate["destination"]["name_template"] = template

    assert expected_code in _issue_codes(_config(candidate))


def test_tar_zst_forbids_password_encryption(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["encryption"] = {
        "mode": "password",
        "password_file": "/etc/vaultkeep/secrets/app.passphrase",
    }

    assert {"encryption_mode", "password_file_forbidden"} <= _issue_codes(_config(candidate))


def test_tar_7z_requires_password_settings_and_format_level(
    valid_config: dict[str, Any],
) -> None:
    candidate = deepcopy(valid_config)
    candidate["archive"] = {"format": "tar.7z", "compression_level": 10}
    candidate["encryption"] = {"mode": "none"}

    assert {
        "encryption_mode",
        "password_file_required",
        "compression_level",
    } <= _issue_codes(_config(candidate))


def test_valid_tar_7z_configuration_passes(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["archive"] = {"format": "tar.7z", "compression_level": 9}
    candidate["encryption"] = {
        "mode": "password",
        "password_file": "/etc/vaultkeep/secrets/app.passphrase",
    }

    validate_semantics(_config(candidate))


def test_retention_requires_an_enabled_tier(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["retention"] = {
        "hourly": 0,
        "daily": 0,
        "weekly": 0,
        "monthly": 0,
        "yearly": 0,
    }

    assert "retention_empty" in _issue_codes(_config(candidate))


@pytest.mark.parametrize(
    ("schedule", "expected_code"),
    [
        (
            {
                "enabled": True,
                "interval": "daily",
                "at": "03:30",
                "window": "01:00-05:00",
            },
            "schedule_time_choice",
        ),
        (
            {"enabled": True, "interval": "daily", "at": "25:00"},
            "schedule_time",
        ),
        (
            {"enabled": True, "interval": "daily", "window": "05:00-01:00"},
            "schedule_window_order",
        ),
        (
            {"enabled": True, "interval": "hourly", "at": "01:05"},
            "schedule_hourly_offset",
        ),
        (
            {"enabled": True, "interval": "weekly", "at": "03:30"},
            "schedule_day",
        ),
        (
            {"enabled": True, "interval": "monthly", "day": "monday", "at": "03:30"},
            "schedule_day",
        ),
        (
            {"enabled": True, "interval": "daily", "day": 1, "at": "03:30"},
            "schedule_day",
        ),
    ],
)
def test_invalid_schedules_are_rejected(
    valid_config: dict[str, Any], schedule: dict[str, object], expected_code: str
) -> None:
    candidate = deepcopy(valid_config)
    candidate["schedule"] = schedule

    assert expected_code in _issue_codes(_config(candidate))


@pytest.mark.parametrize(
    "schedule",
    [
        {
            "enabled": True,
            "interval": "hourly",
            "window": "00:05-00:55",
        },
        {
            "enabled": True,
            "interval": "weekly",
            "day": "Sunday",
            "window": "01:00-06:00",
        },
        {
            "enabled": True,
            "interval": "monthly",
            "day": 1,
            "at": "03:30",
        },
    ],
)
def test_valid_schedule_variants_pass(
    valid_config: dict[str, Any], schedule: dict[str, object]
) -> None:
    candidate = deepcopy(valid_config)
    candidate["schedule"] = schedule

    validate_semantics(_config(candidate))


def test_hook_executable_must_be_absolute_and_arguments_cannot_contain_null(
    valid_config: dict[str, Any],
) -> None:
    candidate = deepcopy(valid_config)
    candidate["hooks"]["before_check"] = {
        "command": ["relative-command", "bad\0argument"],
    }

    assert {"hook_path", "hook_null"} <= _issue_codes(_config(candidate))


@pytest.mark.parametrize("pattern", ["!keep.txt", "dangling\\"])
def test_invalid_exclusion_is_rejected(valid_config: dict[str, Any], pattern: str) -> None:
    candidate = deepcopy(valid_config)
    candidate["exclude"] = [pattern]

    assert "exclusion_pattern" in _issue_codes(_config(candidate))


def test_job_id_must_match_filename(valid_config: dict[str, Any]) -> None:
    assert "job_filename" in _issue_codes(
        _config(valid_config),
        path=Path("/etc/vaultkeep/jobs/different.yaml"),
    )
