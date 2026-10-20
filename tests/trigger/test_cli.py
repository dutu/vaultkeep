from __future__ import annotations

import json
from pathlib import Path

import pytest

from vaultkeep.cli import main
from vaultkeep.version import installed_version


def job(tmp_path: Path, valid_config: dict) -> Path:
    path = tmp_path / "app.yaml"
    path.write_text(json.dumps(valid_config))
    return path


def test_version(capsys) -> None:
    assert main(["--user", "trigger", "--version"]) == 0
    assert capsys.readouterr().out == installed_version() + "\n"


def test_requires_command() -> None:
    with pytest.raises(SystemExit):
        main(["trigger"])


def test_requires_job_config() -> None:
    with pytest.raises(SystemExit):
        main(["--user", "trigger", "status"])


def test_status_without_state(tmp_path: Path, valid_config: dict, monkeypatch, capsys) -> None:
    monkeypatch.setattr("vaultkeep.trigger.cli.state_root", lambda _scope: tmp_path / "state")
    monkeypatch.setattr(
        "vaultkeep.trigger.cli._monitor_runtime_status", lambda _scope: "not running"
    )
    assert main(["--user", "trigger", "--config", str(job(tmp_path, valid_config)), "status"]) == 0
    assert "no recorded instances" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def test_status_corrupt_state_exits_2(tmp_path: Path, valid_config: dict, monkeypatch) -> None:
    root = tmp_path / "state"
    instances = root / "instances"
    instances.mkdir(parents=True)
    (instances / "app.json").write_text("{not json")
    monkeypatch.setattr("vaultkeep.trigger.cli.state_root", lambda _scope: root)
    assert main(["--user", "trigger", "--config", str(job(tmp_path, valid_config)), "status"]) == 2


def test_check_config_valid(tmp_path: Path, valid_config: dict, capsys) -> None:
    assert (
        main(["--user", "trigger", "--config", str(job(tmp_path, valid_config)), "check-config"])
        == 0
    )
    assert "0 enabled" in capsys.readouterr().out


def test_config_can_precede_trigger(tmp_path: Path, valid_config: dict) -> None:
    assert (
        main(["--user", "--config", str(job(tmp_path, valid_config)), "trigger", "check-config"])
        == 0
    )


def test_missing_config_exits_2(tmp_path: Path) -> None:
    assert (
        main(["--user", "trigger", "--config", str(tmp_path / "missing.yaml"), "check-config"]) == 2
    )


def test_monitor_refuses_disabled_trigger(tmp_path: Path, valid_config: dict) -> None:
    assert main(["--user", "trigger", "--config", str(job(tmp_path, valid_config)), "monitor"]) == 2


@pytest.mark.parametrize("identifier", ["../../evil", "a" * 16 + "_../../evil", "a" * 16 + "_bad"])
def test_worker_rejects_invalid_namespace_or_attempt(identifier: str) -> None:
    assert main(["--user", "trigger", "exec-worker", identifier]) == 2


@pytest.mark.parametrize(
    "command",
    ["monitor", "check-config", "status", "install", "update", "disable", "remove", "exec-worker"],
)
def test_trigger_requires_explicit_user_mode_before_any_work(monkeypatch, command, capsys) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("mode rejection must precede configuration, services and state access")

    for name in ("_load", "manage_service", "cmd_exec_worker"):
        monkeypatch.setattr(f"vaultkeep.trigger.cli.{name}", unexpected)
    arguments = ["trigger", "--config", "/missing/job.yaml", command]
    if command == "exec-worker":
        arguments.append("invalid")
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2
    assert "specify --user" in capsys.readouterr().err


@pytest.mark.parametrize(
    "command",
    ["monitor", "check-config", "status", "install", "update", "disable", "remove", "exec-worker"],
)
def test_trigger_rejects_root_before_any_work(monkeypatch, command, capsys) -> None:
    monkeypatch.setattr("vaultkeep.trigger.cli.os.geteuid", lambda: 0)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("root rejection must precede configuration, services and state access")

    for name in ("_load", "manage_service", "cmd_exec_worker"):
        monkeypatch.setattr(f"vaultkeep.trigger.cli.{name}", unexpected)
    arguments = ["--user", "trigger", "--config", "/missing/job.yaml", command]
    if command == "exec-worker":
        arguments.append("invalid")
    assert main(arguments) == 2
    assert "cannot run as root" in capsys.readouterr().err


def test_root_can_inspect_trigger_version_and_help(monkeypatch, capsys) -> None:
    monkeypatch.setattr("vaultkeep.trigger.cli.os.geteuid", lambda: 0)
    assert main(["trigger", "--version"]) == 0
    assert capsys.readouterr().out == installed_version() + "\n"
    with pytest.raises(SystemExit) as error:
        main(["trigger", "--help"])
    assert error.value.code == 0
    assert "user-mode only" in capsys.readouterr().out
