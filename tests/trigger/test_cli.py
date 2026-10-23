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
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == installed_version() + "\n"


def test_requires_command() -> None:
    with pytest.raises(SystemExit):
        main(["trigger"])


def test_requires_job_config() -> None:
    with pytest.raises(SystemExit):
        main(["--user", "trigger", "monitor"])


@pytest.mark.parametrize("extra", [[], ["--runtime"]])
def test_removed_check_config_is_rejected(monkeypatch, capsys, extra) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("removed command must be rejected before loading configuration")

    monkeypatch.setattr("vaultkeep.trigger.cli._load", unexpected)
    with pytest.raises(SystemExit) as error:
        main(["--user", "--config", "/missing/job.yaml", "trigger", "check-config", *extra])
    assert error.value.code == 2
    assert "invalid choice: 'check-config'" in capsys.readouterr().err


def test_monitor_refuses_disabled_trigger(tmp_path: Path, valid_config: dict) -> None:
    assert main(["--user", "--config", str(job(tmp_path, valid_config)), "trigger", "monitor"]) == 2


@pytest.mark.parametrize("identifier", ["../../evil", "a" * 16 + "_../../evil", "a" * 16 + "_bad"])
def test_worker_rejects_invalid_namespace_or_attempt(identifier: str) -> None:
    assert main(["--user", "trigger", "exec-worker", identifier]) == 2


@pytest.mark.parametrize(
    "command",
    ["monitor", "install", "update", "disable", "remove", "exec-worker"],
)
def test_trigger_requires_explicit_user_mode_before_any_work(monkeypatch, command, capsys) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("mode rejection must precede configuration, services and state access")

    for name in ("_load", "manage_service", "cmd_exec_worker"):
        monkeypatch.setattr(f"vaultkeep.trigger.cli.{name}", unexpected)
    arguments = ["--config", "/missing/job.yaml", "trigger", command]
    if command == "exec-worker":
        arguments.append("invalid")
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2
    assert "specify --user" in capsys.readouterr().err


@pytest.mark.parametrize(
    "command",
    ["monitor", "install", "update", "disable", "remove", "exec-worker"],
)
def test_trigger_rejects_root_before_any_work(monkeypatch, command, capsys) -> None:
    monkeypatch.setattr("vaultkeep.trigger.cli.os.geteuid", lambda: 0)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("root rejection must precede configuration, services and state access")

    for name in ("_load", "manage_service", "cmd_exec_worker"):
        monkeypatch.setattr(f"vaultkeep.trigger.cli.{name}", unexpected)
    arguments = ["--user", "--config", "/missing/job.yaml", "trigger", command]
    if command == "exec-worker":
        arguments.append("invalid")
    assert main(arguments) == 2
    assert "cannot run as root" in capsys.readouterr().err


def test_root_can_inspect_trigger_version_and_help(monkeypatch, capsys) -> None:
    monkeypatch.setattr("vaultkeep.trigger.cli.os.geteuid", lambda: 0)
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == installed_version() + "\n"
    with pytest.raises(SystemExit) as error:
        main(["trigger", "--help"])
    assert error.value.code == 0
    help_output = capsys.readouterr().out
    assert "user-mode only" in help_output
    assert "check-config" not in help_output
