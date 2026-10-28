"""Job YAML loading and adaptation to isolated shutdown monitors."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from vaultkeep.application import matcher_from_canonical
from vaultkeep.trigger.config import load_config, validate_vaultkeep_executable
from vaultkeep.trigger.errors import ConfigurationError


def trigger_settings() -> dict:
    return {
        "enabled": True,
    }


def write_job(tmp_path: Path, valid_config: dict, trigger: dict | None = None) -> Path:
    valid_config["trigger"] = trigger if trigger is not None else trigger_settings()
    valid_config.setdefault(
        "application",
        {
            "name": "electrum",
            "data_dir": "/home/user/.electrum",
            "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/electrum/run_electrum"}]},
        },
    )
    path = tmp_path / "app.yaml"
    path.write_text(json.dumps(valid_config))
    return path


def test_loads_job_without_separate_trigger_configuration(
    tmp_path: Path, valid_config: dict
) -> None:
    path = write_job(tmp_path, valid_config)
    config = load_config(path)
    assert config.scope is not None
    assert config.job_id == "app"
    assert len(config.instances) == 1
    instance = config.instances[0]
    assert instance.id == "app"
    assert instance.job.config == path
    assert instance.enabled
    assert config.poll_seconds == 2
    assert config.confirm_seconds == 5
    assert config.vaultkeep.name == "vaultkeep"


def test_runtime_parameters_and_job_identity(tmp_path: Path, valid_config: dict) -> None:
    settings = trigger_settings()
    settings["params"] = {"NETWORK": "mainnet"}
    valid_config["destination"]["root"] += "/{NETWORK}"
    path = write_job(tmp_path, valid_config, settings)
    first = load_config(path)
    second = load_config(path, runtime_params={"NETWORK": "testnet"})
    assert first.instances[0].job.params == {"NETWORK": "mainnet"}
    assert second.instances[0].job.params == {"NETWORK": "testnet"}
    assert first.scope != second.scope
    assert second.scope == load_config(path, runtime_params={"NETWORK": "testnet"}).scope


def test_jobs_have_distinct_scopes(tmp_path: Path, valid_config: dict) -> None:
    path = write_job(tmp_path, valid_config)
    other = tmp_path / "other" / "app.yaml"
    other.parent.mkdir()
    other.write_bytes(path.read_bytes())
    assert load_config(path).scope != load_config(other).scope


def test_trigger_is_disabled_when_omitted(tmp_path: Path, valid_config: dict) -> None:
    path = tmp_path / "app.yaml"
    path.write_text(json.dumps(valid_config))
    assert not load_config(path).enabled_instances()


def test_schedule_and_trigger_are_mutually_exclusive(tmp_path: Path, valid_config: dict) -> None:
    valid_config["schedule"]["enabled"] = True
    with pytest.raises(ConfigurationError, match="mutually exclusive"):
        load_config(write_job(tmp_path, valid_config))


@pytest.mark.parametrize(
    "field,value",
    [
        ("unknown", True),
        ("enabled", "yes"),
        ("app", "other"),
        ("data_dir", "relative"),
        ("poll_seconds", -1),
        ("poll_seconds", True),
        ("confirm_seconds", 0),
        ("deadline_seconds", float("inf")),
        ("params", {"NETWORK": 1}),
        ("params", {"unsafe": "$(command)"}),
    ],
)
def test_invalid_trigger_settings_rejected(
    tmp_path: Path, valid_config: dict, field, value
) -> None:
    settings = trigger_settings()
    settings[field] = value
    with pytest.raises(ConfigurationError):
        load_config(write_job(tmp_path, valid_config, settings))


@pytest.mark.parametrize("field", ["name", "data_dir", "matcher"])
def test_enabled_trigger_requires_identity(tmp_path: Path, valid_config: dict, field) -> None:
    settings = trigger_settings()
    valid_config["application"] = {
        "name": "electrum",
        "data_dir": "/home/user/.electrum",
        "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/electrum/run_electrum"}]},
    }
    del valid_config["application"][field]
    with pytest.raises(ConfigurationError):
        load_config(write_job(tmp_path, valid_config, settings))


@pytest.mark.parametrize(
    "matcher",
    [
        {},
        {"exe_realpath": "/usr/bin/python3"},
        {"argv_tokens": ["--dir"]},
        {"entrypoints": [{"kind": "script", "value": "relative"}]},
        {
            "entrypoints": [{"kind": "script", "value": "/opt/electrum"}],
            "env_eq": {"HOME": "/home/user"},
        },
        {
            "entrypoints": [{"kind": "script", "value": "/opt/electrum"}],
            "cmdline_regex": "[",
        },
        {"entrypoints": []},
        {
            "entrypoints": [
                {"kind": "java_main", "value": "app.Main"},
                {"kind": "java_main", "value": "app.Main"},
            ]
        },
    ],
)
def test_weak_or_invalid_matcher_rejected(tmp_path: Path, valid_config: dict, matcher) -> None:
    settings = trigger_settings()
    valid_config.setdefault(
        "application",
        {
            "name": "electrum",
            "data_dir": "/home/user/.electrum",
        },
    )["matcher"] = matcher
    with pytest.raises(ConfigurationError):
        load_config(write_job(tmp_path, valid_config, settings))


def test_unknown_entrypoint_property_reports_path_and_suggestion(
    tmp_path: Path, valid_config: dict
) -> None:
    settings = trigger_settings()
    valid_config.setdefault(
        "application",
        {
            "name": "electrum",
            "data_dir": "/home/user/.electrum",
        },
    )["matcher"] = {
        "entrypoints": [{"kind": "script", "value": "/opt/electrum/run_electrum", "valeu": "/bad"}]
    }

    with pytest.raises(ConfigurationError) as captured:
        load_config(write_job(tmp_path, valid_config, settings))

    assert "application.matcher.entrypoints[0].valeu" in str(captured.value)
    assert "application.matcher.entrypoints.value" in str(captured.value)


def test_matcher_canonical_roundtrip(tmp_path: Path, valid_config: dict) -> None:
    settings = trigger_settings()
    valid_config.setdefault(
        "application",
        {
            "name": "electrum",
            "data_dir": "/home/user/.electrum",
        },
    ).setdefault(
        "matcher", {"entrypoints": [{"kind": "script", "value": "/opt/electrum/run_electrum"}]}
    )["env_eq"] = {"ELECTRUMDIR": "/home/user/.electrum"}
    matcher = load_config(write_job(tmp_path, valid_config, settings)).instances[0].matcher
    assert matcher_from_canonical(matcher.canonical()) == matcher


def test_alternative_entrypoints_load_and_roundtrip(tmp_path: Path, valid_config: dict) -> None:
    settings = trigger_settings()
    valid_config.setdefault(
        "application",
        {"data_dir": "/home/user/.electrum"},
    )["name"] = "bisq"
    valid_config.setdefault(
        "application",
        {
            "name": "electrum",
            "data_dir": "/home/user/.electrum",
        },
    )["matcher"] = {
        "entrypoints": [
            {"kind": "java_main", "value": "bisq.desktop.app.BisqAppMain"},
            {"kind": "jar", "value": "/opt/bisq-daemon/daemon.jar"},
        ]
    }
    matcher = load_config(write_job(tmp_path, valid_config, settings)).instances[0].matcher

    assert [(entry.kind, entry.value) for entry in matcher.entrypoints] == [
        ("java_main", "bisq.desktop.app.BisqAppMain"),
        ("jar", "/opt/bisq-daemon/daemon.jar"),
    ]
    canonical = matcher.canonical()
    assert len(canonical["entrypoints"]) == 2
    assert matcher_from_canonical(canonical) == matcher


def test_missing_job_reports_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_config(tmp_path / "missing.yaml")


def test_executable_must_be_runnable(tmp_path: Path, valid_config: dict) -> None:
    config = load_config(write_job(tmp_path, valid_config))
    with pytest.raises(ConfigurationError):
        validate_vaultkeep_executable(replace(config, vaultkeep=tmp_path / "missing"))
    validate_vaultkeep_executable(config)
