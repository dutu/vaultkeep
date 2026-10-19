from __future__ import annotations

import json
from pathlib import Path

from vaultkeep_trigger.cli import main
from vaultkeep_trigger.version import TRIGGER_VERSION


def _config_text(vaultkeep: Path, job: Path) -> str:
    return f"""
[trigger]
poll_seconds = 1
confirm_seconds = 1
vaultkeep = "{vaultkeep}"

[[instance]]
id = "electrum-main"
app = "electrum"
enabled = true
data_dir = "/home/user/.electrum"

[instance.matcher]
entrypoint = {{ kind = "script", value = "/opt/app/run_app" }}

[instance.job]
config = "{job}"
"""


def test_version(capsys) -> None:
    assert main(["--version"]) == 0
    text: str = capsys.readouterr().out
    assert TRIGGER_VERSION in text


def test_requires_command() -> None:
    import pytest

    with pytest.raises(SystemExit):
        main([])


def test_status_without_state(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("vaultkeep_trigger.cli.state_root", lambda: tmp_path / "state")
    monkeypatch.setattr("vaultkeep_trigger.cli._monitor_runtime_status", lambda: "not running")
    assert main(["status"]) == 0
    text: str = capsys.readouterr().out
    assert "no recorded instances" in text


def test_status_corrupt_state_exits_2(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "state"
    instances = root / "instances"
    instances.mkdir(parents=True)
    (instances / "electrum-main.json").write_text("{not json")
    monkeypatch.setattr("vaultkeep_trigger.cli.state_root", lambda: root)
    monkeypatch.setattr("vaultkeep_trigger.cli._monitor_runtime_status", lambda: "not running")
    assert main(["status"]) == 2


def test_status_unknown_instance_exits_2(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "state"
    instances = root / "instances"
    instances.mkdir(parents=True)
    (instances / "electrum-main.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "instance_id": "electrum-main",
                "armed": False,
            }
        )
    )
    monkeypatch.setattr("vaultkeep_trigger.cli.state_root", lambda: root)
    monkeypatch.setattr("vaultkeep_trigger.cli._monitor_runtime_status", lambda: "not running")
    assert main(["status", "does-not-exist"]) == 2


def test_check_config_valid(tmp_path: Path, capsys) -> None:
    script = tmp_path / "vaultkeep"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    job = tmp_path / "job.yaml"
    job.write_text("job: {}\n", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(_config_text(script, job), encoding="utf-8")
    assert main(["--config", str(config), "check-config"]) == 0
    text: str = capsys.readouterr().out
    assert "instances: 1 (1 enabled)" in text


def test_check_config_missing_file(tmp_path: Path) -> None:
    assert main(["--config", str(tmp_path / "nope.toml"), "check-config"]) == 2


def test_check_config_runtime_blocks_without_manager(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "vaultkeep"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    job = tmp_path / "job.yaml"
    job.write_text("job: {}\n", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(_config_text(script, job), encoding="utf-8")

    class _Unreachable:
        def manager_reachable(self) -> bool:
            return False

    monkeypatch.setattr("vaultkeep_trigger.cli.RealSystemdController", lambda: _Unreachable())
    assert main(["--config", str(config), "check-config", "--runtime"]) == 3
