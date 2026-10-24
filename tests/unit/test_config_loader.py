"""Tests for safe YAML loading and schema error reporting."""

from __future__ import annotations

from pathlib import Path

import pytest

from vaultkeep.config import load_config
from vaultkeep.errors import ConfigurationError


def _write_config(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "duplicate.yaml",
        "config_version: 1\nconfig_version: 1\n",
    )

    with pytest.raises(ConfigurationError, match="duplicate key"):
        load_config(path)


def test_yaml_root_must_be_mapping(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "list.yaml", "- one\n- two\n")

    with pytest.raises(ConfigurationError) as captured:
        load_config(path)

    assert captured.value.issues[0].code == "mapping_type"
    assert captured.value.issues[0].dotted_path == "<root>"


def test_unsafe_yaml_tag_is_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "unsafe.yaml",
        "!!python/object/apply:os.system ['echo unsafe']\n",
    )

    with pytest.raises(ConfigurationError) as captured:
        load_config(path)

    assert captured.value.issues[0].code == "yaml_parse"


def test_unknown_property_reports_path_and_suggestion(
    tmp_path: Path, valid_config: dict[str, object]
) -> None:
    import json

    logging = valid_config["logging"]
    assert isinstance(logging, dict)
    logging["levle"] = logging.pop("level")
    path = _write_config(tmp_path / "app.yaml", json.dumps(valid_config))

    with pytest.raises(ConfigurationError) as captured:
        load_config(path)

    issue = captured.value.issues[0]
    assert issue.dotted_path == "logging.levle"
    assert issue.message == "Unknown property. Did you mean: logging.level?"


def test_schema_collects_independent_errors(
    tmp_path: Path, valid_config: dict[str, object]
) -> None:
    import json

    schedule = valid_config["schedule"]
    retention = valid_config["retention"]
    assert isinstance(schedule, dict)
    assert isinstance(retention, dict)
    schedule["enabled"] = "yes"
    retention["hourly"] = "24"
    path = _write_config(tmp_path / "app.yaml", json.dumps(valid_config))

    with pytest.raises(ConfigurationError) as captured:
        load_config(path)

    assert {issue.dotted_path for issue in captured.value.issues} == {
        "retention.hourly",
        "schedule.enabled",
    }


def test_missing_file_is_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Unable to parse"):
        load_config(tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    ("parent", "unknown", "expected"),
    [
        ((), "notificatons", "notifications"),
        (("notifications",), "comand", "notifications.command"),
        (("notifications",), "timout_ms", "notifications.timeout_ms"),
        (("notifications",), "bakcup", "notifications.backup"),
        (("notifications",), "triger", "notifications.trigger"),
        (("notifications", "backup"), "enabld", "notifications.backup.enabled"),
        (("notifications", "trigger"), "enabld", "notifications.trigger.enabled"),
        (("trigger",), "notificatons", None),
        (("trigger", "notifications"), "enabld", None),
    ],
)
def test_notification_typo_suggestions_follow_current_schema(
    tmp_path: Path, valid_config: dict, parent: tuple[str, ...], unknown: str, expected: str | None
) -> None:
    import json

    section = valid_config
    for key in parent:
        section = section.setdefault(key, {})
    section[unknown] = True
    path = _write_config(tmp_path / "app.yaml", json.dumps(valid_config))

    with pytest.raises(ConfigurationError) as captured:
        load_config(path)

    issue = captured.value.issues[0]
    assert issue.code == "extra_forbidden"
    assert issue.message == (
        f"Unknown property. Did you mean: {expected}?" if expected else "Unknown property."
    )


@pytest.mark.parametrize("value", [None, "/mnt/backups"])
def test_removed_destination_field_is_rejected(
    tmp_path: Path, valid_config: dict[str, object], value: str | None
) -> None:
    import json

    destination = valid_config["destination"]
    assert isinstance(destination, dict)
    destination["mount_point"] = value
    path = _write_config(tmp_path / "app.yaml", json.dumps(valid_config))

    with pytest.raises(ConfigurationError) as captured:
        load_config(path)

    assert len(captured.value.issues) == 1
    issue = captured.value.issues[0]
    assert issue.dotted_path == "destination.mount_point"
    assert issue.code == "extra_forbidden"
    assert issue.message == "Unknown property."
