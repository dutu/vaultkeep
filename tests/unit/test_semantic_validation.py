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


def _issue_codes(
    config: JobConfig, *, path: Path | None = None, runtime_params: dict[str, str] | None = None
) -> set[str]:
    with pytest.raises(ConfigurationError) as captured:
        validate_semantics(config, config_path=path, runtime_params=runtime_params)
    return {issue.code for issue in captured.value.issues}


def test_valid_default_configuration_passes(valid_config: dict[str, Any]) -> None:
    validate_semantics(_config(valid_config))


def test_semantics_collect_path_and_overlap_errors(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["sources"] = [
        {"path": "relative", "archive_path_mode": "prefix", "archive_prefix": "relative"},
        {"path": "/srv/app", "archive_path_mode": "prefix", "archive_prefix": "app"},
        {"path": "/srv/app/config", "archive_path_mode": "prefix", "archive_prefix": "config"},
    ]
    candidate["destination"]["root"] = "/srv/app/backups"

    codes = _issue_codes(_config(candidate))

    assert {"path_absolute", "source_overlap", "destination_source_overlap"} <= codes


def test_duplicate_normalized_sources_are_rejected(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["sources"] = [
        {"path": "/srv/app", "archive_path_mode": "prefix", "archive_prefix": "app"},
        {"path": "/srv/other/../app", "archive_path_mode": "prefix", "archive_prefix": "app2"},
    ]

    assert "source_duplicate" in _issue_codes(_config(candidate))


def test_source_archive_path_config_is_validated(valid_config: dict[str, Any]) -> None:
    unsafe_prefix = deepcopy(valid_config)
    unsafe_prefix["sources"][0]["archive_prefix"] = "../outside"

    assert "archive_prefix_path" in _issue_codes(_config(unsafe_prefix))


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
    ("mount_point", "expected_code"),
    [("relative/mount", "path_absolute"), ("a\0b", "path_null")],
)
def test_mount_point_must_be_absolute(
    valid_config: dict[str, Any], mount_point: str, expected_code: str
) -> None:
    candidate = deepcopy(valid_config)
    candidate["destination"]["mount_point"] = mount_point

    assert expected_code in _issue_codes(_config(candidate))


def test_mount_point_must_contain_destination_root(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["destination"]["root"] = "/mnt/backups/app"
    candidate["destination"]["mount_point"] = "/mnt/other"

    assert "mount_point_parent" in _issue_codes(_config(candidate))


@pytest.mark.parametrize(
    ("template", "expected_code"),
    [
        ("backup-{job}", "template_timestamp"),
        ("backup-{date}-{timestamp_utc}", "param_missing"),
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


def test_runtime_parameter_placeholders_are_accepted_when_supplied(
    valid_config: dict[str, Any],
) -> None:
    candidate = deepcopy(valid_config)
    candidate["destination"]["name_template"] = "backup-{job}-{repo}-{timestamp_utc:%Y%m%dT%H%M%SZ}"

    validate_semantics(_config(candidate), runtime_params={"repo": "nas-a"})


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
        (
            {"enabled": True, "at": "03:30"},
            "schedule_interval",
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


def test_disabled_schedule_requires_only_enabled(valid_config: dict[str, Any]) -> None:
    candidate = deepcopy(valid_config)
    candidate["schedule"] = {"enabled": False}

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
