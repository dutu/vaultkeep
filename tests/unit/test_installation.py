"""Custom layouts survive clean service environments and literal unit arguments."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from vaultkeep import installation
from vaultkeep.backup.paths import WorkflowPaths
from vaultkeep.errors import LockError
from vaultkeep.locking import deployment_lock
from vaultkeep.maintenance import command_selection
from vaultkeep.schedule import TimerManager, TimerPaths
from vaultkeep.trigger.config import load_config
from vaultkeep.trigger.service import render_service


@pytest.fixture
def layout(tmp_path, monkeypatch):
    paths = {name: str(tmp_path / name.lower()) for name in installation.DEFAULT_PATHS}
    release = tmp_path / "release"
    release.mkdir()
    (release / "deployment.json").write_text(json.dumps({"layout": paths}))
    monkeypatch.setattr(installation.sys, "prefix", str(release / "venv"))
    for name in paths:
        monkeypatch.delenv(name, raising=False)
    return paths


def test_installed_layout_without_installer_environment(layout, valid_config, tmp_path):
    paths = TimerPaths()
    assert paths.jobs_root == Path(layout["VAULTKEEP_CONFIG_ROOT"]) / "jobs"
    assert paths.units_root == Path(layout["VAULTKEEP_SYSTEMD_ROOT"])
    assert paths.registry_path == Path(layout["VAULTKEEP_VAR_ROOT"]) / "systemd-instances.json"
    assert WorkflowPaths().state_root == Path(layout["VAULTKEEP_VAR_ROOT"]) / "jobs"
    assert WorkflowPaths().local_temp_root == Path(layout["VAULTKEEP_VAR_ROOT"]) / "tmp"

    config = tmp_path / "app.yaml"
    valid_config["application"] = {
        "name": "electrum",
        "data_dir": "/home/user/.electrum",
        "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/electrum"}]},
    }
    valid_config["trigger"] = {"enabled": True}
    config.write_text(json.dumps(valid_config))
    monitor = render_service(load_config(config))
    assert f"ExecStart={layout['VAULTKEEP_BIN_LINK']} --user" in monitor
    assert command_selection(monitor, monitor=True, user=True) == (config, {})
    for user in (False, True):
        service = TimerManager(user_mode=user)._service_content(config, "app", {"repo": "nas"})
        assert f"ExecStart={layout['VAULTKEEP_BIN_LINK']} " in service
        assert command_selection(service, monitor=False, user=user) == (config, {"repo": "nas"})


def test_installed_layout_shares_deployment_lock(layout):
    prefix = Path(layout["VAULTKEEP_INSTALL_PREFIX"])
    prefix.mkdir()
    lock = prefix / "maintenance.lock"
    with lock.open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            pytest.raises(LockError, match="installation/update is in progress"),
            deployment_lock(),
        ):
            pytest.fail("custom installation lock must be held")


def test_explicit_path_override(layout, monkeypatch):
    monkeypatch.setenv("VAULTKEEP_BIN_LINK", "/custom/bin/vaultkeep")
    assert installation.installation_path("VAULTKEEP_BIN_LINK") == Path("/custom/bin/vaultkeep")


@pytest.mark.parametrize("metadata", [None, {"schema_version": 3}])
def test_uninstalled_and_older_releases_keep_default_paths(tmp_path, monkeypatch, metadata):
    monkeypatch.setattr(installation.sys, "prefix", str(tmp_path / "venv"))
    if metadata is not None:
        (tmp_path / "deployment.json").write_text(json.dumps(metadata))
    for name, default in installation.DEFAULT_PATHS.items():
        monkeypatch.delenv(name, raising=False)
        assert installation.installation_path(name) == Path(default)


@pytest.mark.parametrize("value", ["relative/bin", "/srv/../bin", "/srv//bin", "/srv/bin\nunit"])
def test_invalid_layout_paths_are_rejected(monkeypatch, value):
    monkeypatch.setenv("VAULTKEEP_BIN_LINK", value)
    with pytest.raises(ValueError, match="absolute and normalized"):
        installation.installation_path("VAULTKEEP_BIN_LINK")


@pytest.mark.parametrize("value", ["/bin/quote'", '/bin/quote"', r"/bin/back\slash"])
def test_systemd_incompatible_executable_paths_are_rejected(monkeypatch, value):
    monkeypatch.setenv("VAULTKEEP_BIN_LINK", value)
    with pytest.raises(ValueError, match="quotes or backslashes"):
        installation.installation_path("VAULTKEEP_BIN_LINK")


@pytest.mark.parametrize("directory", ["custom", "space 'quote\" back\\x20 %n ${HOME}"])
def test_shared_templates_preserve_custom_paths_and_only_expand_instance(
    tmp_path, monkeypatch, directory
):
    binary = tmp_path / "custom bin %n ${HOME}/vaultkeep"
    config = tmp_path / directory / "config"
    monkeypatch.setenv("VAULTKEEP_BIN_LINK", str(binary))
    monkeypatch.setenv("VAULTKEEP_CONFIG_ROOT", str(config))
    output = tmp_path / "units"
    output.mkdir()
    source = Path(__file__).resolve().parents[2] / "systemd"
    installation.render_templates(source, output)
    service = (output / "vaultkeep@.service").read_text()
    assert "@VAULTKEEP_" not in service
    assert f"ConditionPathExists={str(config).replace('%', '%%')}/jobs/%i.yaml\n" in service
    # Substitute only the final instance specifier, preserving escaped literal %n.
    selected = service.replace("/%i.yaml", "/app.yaml")
    assert command_selection(selected, monitor=False, user=False) == (
        config / "jobs/app.yaml",
        {},
    )
    worker = (output / "vaultkeep-trigger-exec@.service").read_text()
    executable = installation.systemd_argument(str(binary), executable=True)
    assert f"ExecStart={executable} --user trigger exec-worker %i" in worker


@pytest.mark.parametrize("user", [False, True])
def test_schedule_uses_literal_custom_binary_and_config(tmp_path, monkeypatch, user):
    binary = tmp_path / "space %n ${HOME}" / "vaultkeep"
    config = tmp_path / "space 'quote\" back\\x20 %n ${HOME}" / "app.yaml"
    monkeypatch.setenv("VAULTKEEP_BIN_LINK", str(binary))
    content = TimerManager(user_mode=user)._service_content(config, "app", {"repo": "nas"})
    assert command_selection(content, monitor=False, user=user) == (config, {"repo": "nas"})


def test_parser_accepts_legacy_default_and_rejects_unrelated_executable(monkeypatch):
    monkeypatch.setenv("VAULTKEEP_BIN_LINK", "/custom/bin/vaultkeep")
    command = "ExecStart=/usr/local/bin/vaultkeep --config /jobs/app.yaml run\n"
    assert command_selection(command, monitor=False, user=False) == (Path("/jobs/app.yaml"), {})
    with pytest.raises(ValueError, match="unexpected managed service executable"):
        command_selection(
            command.replace("/usr/local/bin/vaultkeep", "/other/bin/vaultkeep"),
            monitor=False,
            user=False,
        )


@pytest.mark.parametrize("kind", ["backup", "worker", "user-schedule", "monitor"])
@pytest.mark.parametrize(
    "directory_name",
    ["space here", "percent%n", "dollar${HOME}"],
)
def test_custom_executable_survives_systemd_parsing(
    tmp_path, monkeypatch, kind, directory_name, valid_config
):
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze is required to verify ExecStart parsing")
    directory = tmp_path / directory_name
    directory.mkdir()
    binary = directory / "vaultkeep"
    binary.symlink_to("/usr/bin/true")
    config = tmp_path / "space 'quote\" back\\x20 %n ${HOME}" / "config"
    monkeypatch.setenv("VAULTKEEP_BIN_LINK", str(binary))
    monkeypatch.setenv("VAULTKEEP_CONFIG_ROOT", str(config))
    installation.render_templates(Path(__file__).resolve().parents[2] / "systemd", tmp_path)
    if kind == "monitor":
        config.mkdir(parents=True)
        job = config / "app.yaml"
        valid_config["application"] = {
            "name": "electrum",
            "data_dir": "/home/user/.electrum",
            "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/electrum"}]},
        }
        valid_config["trigger"] = {"enabled": True}
        job.write_text(json.dumps(valid_config))
        unit = tmp_path / "vaultkeep-test.service"
        unit.write_text(render_service(load_config(job)))
        expected = [
            str(binary),
            "--user",
            "--config",
            str(job),
            "trigger",
            "monitor",
            "--expected-identity",
            load_config(job).scope,
        ]
    elif kind == "user-schedule":
        unit = tmp_path / "vaultkeep-test.service"
        unit.write_text(
            TimerManager(user_mode=True)._service_content(config / "app.yaml", "app", {})
        )
        expected = [str(binary), "--user", "--config", str(config / "app.yaml"), "run"]
    elif kind == "backup":
        unit = tmp_path / "vaultkeep@app.service"
        unit.write_text((tmp_path / "vaultkeep@.service").read_text())
        expected = [str(binary), "--config", str(config / "jobs/app.yaml"), "run"]
    else:
        unit = tmp_path / "vaultkeep-trigger-exec@test.service"
        unit.write_text((tmp_path / "vaultkeep-trigger-exec@.service").read_text())
        expected = [str(binary), "--user", "trigger", "exec-worker", "test"]
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    result = subprocess.run(
        [analyzer, "--user", "--man=no", "verify", str(unit)],
        env={
            **os.environ,
            "XDG_RUNTIME_DIR": str(runtime),
            "SYSTEMD_LOG_LEVEL": "debug",
            "SYSTEMD_LOG_COLOR": "0",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = result.stdout + result.stderr
    if "SO_PASSCRED failed: Operation not permitted" in output:
        pytest.skip("sandbox blocks systemd-analyze sockets; run outside that sandbox")
    assert result.returncode == 0, "\n".join(
        line
        for line in output.splitlines()
        if "special characters" in line or "error" in line.lower()
    )
    commands = [
        line.partition("Command Line: ")[2]
        for line in output.splitlines()
        if "Command Line: " in line
    ]
    assert len(commands) == 1, output
    argv = [arg.replace(r"\$", "$").replace("$$", "$") for arg in shlex.split(commands[0])]
    assert argv == expected
