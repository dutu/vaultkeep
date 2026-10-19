from __future__ import annotations

from pathlib import Path

import pytest

from vaultkeep_trigger.config import (
    TriggerConfig,
    load_config,
    matcher_from_canonical,
    validate_vaultkeep_executable,
)
from vaultkeep_trigger.errors import ConfigurationError


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


VALID = """
[trigger]
poll_seconds = 2
confirm_seconds = 5
vaultkeep = "/usr/local/bin/vaultkeep"

[[instance]]
id = "electrum-main"
app = "electrum"
enabled = false
data_dir = "/home/user/.electrum"

[instance.matcher]
entrypoint = { kind = "script", value = "/opt/electrum/run_electrum" }

[instance.job]
config = "/jobs/electrum.yaml"
params = { NETWORK = "mainnet" }
"""


def test_loads_valid_config(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, VALID))
    assert isinstance(config, TriggerConfig)
    assert config.poll_seconds == 2
    assert len(config.instances) == 1
    instance = config.instances[0]
    assert instance.id == "electrum-main"
    assert instance.enabled is False
    assert instance.job.params == {"NETWORK": "mainnet"}
    assert instance.matcher.entrypoint is not None


def test_deadline_inheritance(tmp_path: Path) -> None:
    text = VALID.replace("confirm_seconds = 5", "confirm_seconds = 5\ndeadline_seconds = 60")
    config = load_config(_write(tmp_path, text))
    assert config.deadline_seconds == 60
    assert config.instances[0].deadline_seconds == 60


def test_per_instance_override(tmp_path: Path) -> None:
    text = """
[trigger]
poll_seconds = 2
confirm_seconds = 5
vaultkeep = "/usr/local/bin/vaultkeep"

[[instance]]
id = "electrum-main"
app = "electrum"
enabled = false
data_dir = "/home/user/.electrum"
poll_seconds = 9
confirm_seconds = 3
matcher = { entrypoint = { kind = "script", value = "/opt/electrum/run_electrum" } }
job = { config = "/jobs/electrum.yaml" }
"""
    config = load_config(_write(tmp_path, text))
    assert config.instances[0].poll_seconds == 9
    assert config.instances[0].confirm_seconds == 3


def test_unknown_key_rejected(tmp_path: Path) -> None:
    text = VALID.replace("poll_seconds = 2", "poll_seconds = 2\nbogus = 1")
    with pytest.raises(ConfigurationError):
        load_config(_write(tmp_path, text))


def test_unknown_instance_key_rejected(tmp_path: Path) -> None:
    text = VALID + "\nunknown_field = 3\n"
    with pytest.raises(ConfigurationError):
        load_config(_write(tmp_path, text))


def test_duplicate_ids_rejected(tmp_path: Path) -> None:
    text = """
[trigger]
vaultkeep = "/usr/local/bin/vaultkeep"

[[instance]]
id = "dup"
app = "electrum"
data_dir = "/home/user/.electrum"
matcher = { entrypoint = { kind = "script", value = "/opt/electrum/run_electrum" } }
job = { config = "/jobs/electrum.yaml" }

[[instance]]
id = "dup"
app = "electrum"
data_dir = "/home/user/.electrum"
matcher = { entrypoint = { kind = "script", value = "/opt/electrum/run_electrum" } }
job = { config = "/jobs/electrum.yaml" }
"""
    with pytest.raises(ConfigurationError, match="duplicated"):
        load_config(_write(tmp_path, text))


def test_unsafe_id_rejected(tmp_path: Path) -> None:
    text = VALID.replace('id = "electrum-main"', 'id = "../evil"')
    with pytest.raises(ConfigurationError):
        load_config(_write(tmp_path, text))


def test_weak_matcher_rejected(tmp_path: Path) -> None:
    text = VALID.replace(
        'entrypoint = { kind = "script", value = "/opt/electrum/run_electrum" }',
        'argv_tokens = ["--dir"]',
    )
    with pytest.raises(ConfigurationError, match="strong identity"):
        load_config(_write(tmp_path, text))


@pytest.mark.parametrize(
    "identity",
    [
        "",
        'exe_realpath = "/usr/bin/python3"',
        'exe_realpath = "/usr/bin/java"',
        'exe_realpath = "/opt/wasabi/WalletWasabi"',
    ],
)
def test_empty_or_interpreter_only_matcher_rejected(tmp_path: Path, identity: str) -> None:
    text = VALID.replace(
        'entrypoint = { kind = "script", value = "/opt/electrum/run_electrum" }', identity
    )
    with pytest.raises(ConfigurationError, match="strong identity"):
        load_config(_write(tmp_path, text))


def test_interpreter_with_application_entrypoint_accepted(tmp_path: Path) -> None:
    text = VALID.replace(
        "[instance.matcher]", '[instance.matcher]\nexe_realpath="/usr/bin/python3"'
    )
    assert load_config(_write(tmp_path, text)).instances[0].matcher.entrypoint is not None


def test_disallowed_env_key_rejected(tmp_path: Path) -> None:
    text = VALID.replace(
        'entrypoint = { kind = "script", value = "/opt/electrum/run_electrum" }',
        'entrypoint = { kind = "script", value = "/opt/electrum/run_electrum" }\n'
        'env_eq = { HOME = "/home/user" }',
    )
    with pytest.raises(ConfigurationError, match="env_eq"):
        load_config(_write(tmp_path, text))


def test_allowed_env_key_accepted(tmp_path: Path) -> None:
    text = VALID.replace(
        'entrypoint = { kind = "script", value = "/opt/electrum/run_electrum" }',
        'entrypoint = { kind = "script", value = "/opt/electrum/run_electrum" }\n'
        'env_eq = { ELECTRUMDIR = "/home/user/.electrum" }',
    )
    config = load_config(_write(tmp_path, text))
    assert config.instances[0].matcher.env_eq == {"ELECTRUMDIR": "/home/user/.electrum"}


def test_invalid_timing_rejected(tmp_path: Path) -> None:
    text = VALID.replace("poll_seconds = 2", "poll_seconds = -1")
    with pytest.raises(ConfigurationError):
        load_config(_write(tmp_path, text))


def test_invalid_log_level_rejected(tmp_path: Path) -> None:
    text = VALID.replace("poll_seconds = 2", 'poll_seconds = 2\nlog_level = "loud"')
    with pytest.raises(ConfigurationError):
        load_config(_write(tmp_path, text))


def test_missing_file_reports_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_config(tmp_path / "missing.toml")


def test_matcher_from_canonical_roundtrip(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, VALID))
    canonical = config.instances[0].matcher.canonical()
    rebuilt = matcher_from_canonical(canonical)
    assert rebuilt == config.instances[0].matcher


def test_validate_executable_requires_executable_file(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, VALID))
    with pytest.raises(ConfigurationError):
        validate_vaultkeep_executable(config)
    script = tmp_path / "vaultkeep"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    import dataclasses

    ok = dataclasses.replace(config, vaultkeep=script)
    validate_vaultkeep_executable(ok)
