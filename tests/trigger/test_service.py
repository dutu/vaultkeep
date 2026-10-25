"""Per-job service ownership, explicit activation and recovery isolation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.trigger.conftest import FakeController, FakeRunner, make_config
from vaultkeep.cli import main
from vaultkeep.trigger import service
from vaultkeep.trigger.config import load_config
from vaultkeep.trigger.errors import BlockedError, ConfigurationError
from vaultkeep.trigger.executor import WorkerResult
from vaultkeep.trigger.models import AttemptOutcome, new_uuid
from vaultkeep.trigger.monitor import MonitorLock, unit_name_for
from vaultkeep.trigger.paths import monitor_lock_path, state_root
from vaultkeep.trigger.preflight import runtime_preflight


@pytest.fixture(autouse=True)
def isolated_services(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr("vaultkeep.trigger.paths.account_home", lambda: tmp_path)
    monkeypatch.setattr(
        "vaultkeep.schedule.status.unit_properties",
        lambda unit, **_: {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "SubState": "dead",
            "UnitFileState": "disabled",
            "FragmentPath": str(service.units_root() / unit),
            "DropInPaths": "",
            "NeedDaemonReload": "no",
        },
    )


def config(tmp_path: Path, valid_config: dict):
    valid_config["trigger"] = {
        "enabled": True,
        "app": "electrum",
        "data_dir": "/home/user/.electrum",
        "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/electrum"}]},
    }
    path = tmp_path / "app.yaml"
    path.write_text(json.dumps(valid_config))
    return load_config(path)


def test_distinct_jobs_can_hold_monitor_locks_together(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr("vaultkeep.trigger.paths.account_home", lambda: tmp_path)
    first, second = "a" * 16, "b" * 16
    with (
        MonitorLock(monitor_lock_path(first)),
        MonitorLock(monitor_lock_path(second)),
        pytest.raises(BlockedError, match="another monitor"),
    ):
        MonitorLock(monitor_lock_path(first)).acquire()
    assert state_root(first) != state_root(second)
    attempt = new_uuid()
    assert unit_name_for(attempt, first) != unit_name_for(attempt, second)


def test_worker_uses_only_the_namespace_encoded_in_its_unit(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("vaultkeep.trigger.cli.state_root", lambda scope: tmp_path / scope)

    def execute(attempt, *, store):
        calls.append((attempt, store.root))
        return WorkerResult(0, AttemptOutcome.CREATED, "created")

    monkeypatch.setattr("vaultkeep.trigger.cli.exec_worker", execute)
    attempt = new_uuid()
    for scope in ("a" * 16, "b" * 16):
        assert main(["--user", "trigger", "exec-worker", f"{scope}_{attempt}"]) == 0
    assert calls == [
        (attempt, tmp_path / ("a" * 16)),
        (attempt, tmp_path / ("b" * 16)),
    ]


def test_service_install_disable_and_remove_preserve_state(
    tmp_path: Path, valid_config: dict, monkeypatch
) -> None:
    job = config(tmp_path, valid_config)
    root = tmp_path / "units"
    monkeypatch.setattr(service, "units_root", lambda: root)
    calls = []
    monkeypatch.setattr(service, "_systemctl", lambda *args: calls.append(args) or "")
    unit = service.service_name(job)
    service.manage_service(job, "install")
    content = (root / unit).read_text()
    assert f"vaultkeep --user --config {job.source_path} trigger monitor" in content
    assert "exec-worker" not in content and "PartOf=" not in content
    assert calls == [("daemon-reload",), ("enable", "--now", unit)]
    service.manage_service(job, "disable")
    assert (root / unit).exists()
    assert calls[-1] == ("disable", "--now", unit)
    service.manage_service(job, "remove")
    assert not (root / unit).exists()
    assert all("exec@" not in " ".join(call) for call in calls)


def test_service_management_refuses_unmanaged_files(
    tmp_path: Path, valid_config: dict, monkeypatch
) -> None:
    job = config(tmp_path, valid_config)
    monkeypatch.setattr(service, "units_root", lambda: tmp_path)
    path = tmp_path / service.service_name(job)
    path.write_text("# operator-owned unit\n")
    with pytest.raises(ConfigurationError, match="unmanaged"):
        service.manage_service(job, "install")
    assert path.read_text() == "# operator-owned unit\n"


def test_service_cannot_activate_a_disabled_trigger(
    tmp_path: Path, valid_config: dict, monkeypatch
) -> None:
    job = config(tmp_path, valid_config)
    job = replace(job, instances=())
    root = tmp_path / "units"
    monkeypatch.setattr(service, "units_root", lambda: root)
    with pytest.raises(ConfigurationError, match=r"trigger\.enabled"):
        service.manage_service(job, "install")
    assert not root.exists()


def test_legacy_recovery_evidence_blocks_new_monitors(tmp_path: Path, monkeypatch) -> None:
    legacy = tmp_path / "legacy"
    (legacy / "instances").mkdir(parents=True)
    evidence = legacy / "instances/app.json"
    evidence.write_text('{"review_required":true}')
    binary = tmp_path / "vaultkeep"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    monkeypatch.setattr("vaultkeep.trigger.preflight.state_root", lambda: legacy)
    candidate = replace(make_config(), scope="a" * 16, vaultkeep=binary)
    with pytest.raises(BlockedError, match="legacy shared Trigger state"):
        runtime_preflight(candidate, FakeController(), tmp_path / "new", runner=FakeRunner([]))
    assert evidence.read_text() == '{"review_required":true}'
    assert not (tmp_path / "new").exists()


def test_root_schedule_and_user_trigger_coexist(
    tmp_path: Path, valid_config: dict, monkeypatch
) -> None:
    from copy import deepcopy

    from vaultkeep.schedule import SERVICE_TEMPLATE, TIMER_TEMPLATE, TimerManager, TimerPaths

    root_config = deepcopy(valid_config)
    root_config["schedule"]["enabled"] = True
    root_jobs = tmp_path / "etc/vaultkeep/jobs"
    root_jobs.mkdir(parents=True)
    root_job = root_jobs / "app.yaml"
    root_job.write_text(json.dumps(root_config))
    root_units = tmp_path / "etc/systemd/system"
    root_units.mkdir(parents=True)
    (root_units / "vaultkeep@.timer").write_text(TIMER_TEMPLATE)
    (root_units / "vaultkeep@.service").write_text(SERVICE_TEMPLATE)
    registry = tmp_path / "var/lib/vaultkeep/systemd-instances.json"
    manager = TimerManager(TimerPaths(root_jobs, root_units, registry))
    root_commands = []
    monkeypatch.setattr(
        manager, "_run", lambda command, check=True: root_commands.append(command) or ""
    )
    manager.install(root_job)
    scheduled_files = {path: path.read_bytes() for path in root_units.rglob("*") if path.is_file()}
    registry_before = registry.read_bytes()

    user_jobs = tmp_path / "home/alice/.config/vaultkeep/jobs"
    user_jobs.mkdir(parents=True)
    triggered = config(user_jobs, deepcopy(valid_config))
    user_units = tmp_path / "home/alice/.config/systemd/user"
    monkeypatch.setattr(service, "units_root", lambda: user_units)
    user_commands = []
    monkeypatch.setattr(service, "_systemctl", lambda *args: user_commands.append(args) or "")
    service.manage_service(triggered, "install")
    monitor = service.service_name(triggered)
    assert (user_units / monitor).is_file()
    assert "vaultkeep --user --config" in (user_units / monitor).read_text()
    assert ("systemctl", "enable", "--now", "vaultkeep@app.timer") in root_commands
    assert ("enable", "--now", monitor) in user_commands
    service.manage_service(triggered, "remove")
    assert {
        path: path.read_bytes() for path in root_units.rglob("*") if path.is_file()
    } == scheduled_files
    assert registry.read_bytes() == registry_before


@pytest.mark.parametrize(
    "directory",
    [
        "space here",
        "apostrophe's",
        'double"quote',
        r"back\slash",
        "percent%name",
        "dollar${HOME}",
        "space 'quote\" back\\slash %n ${HOME}",
    ],
)
def test_monitor_config_path_survives_systemd_parsing(
    tmp_path: Path, valid_config: dict, directory: str
) -> None:
    import os
    import shlex
    import shutil
    import subprocess

    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze is required to verify ExecStart parsing")
    job = config(tmp_path, valid_config)
    source = tmp_path / directory / "job.yaml"
    candidate = replace(job, source_path=source)
    unit = tmp_path / service.service_name(candidate)
    # Validate the full command without needing or launching an installed Vaultkeep.
    unit.write_text(
        service.render_service(candidate).replace("/usr/local/bin/vaultkeep", "/usr/bin/true")
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    result = subprocess.run(
        [analyzer, "--user", "--man=no", "verify", str(unit)],
        env=dict(
            os.environ,
            XDG_RUNTIME_DIR=str(runtime),
            SYSTEMD_LOG_LEVEL="debug",
            SYSTEMD_LOG_COLOR="0",
        ),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    output = result.stdout + result.stderr
    if "SO_PASSCRED failed: Operation not permitted" in output:
        pytest.skip("sandbox blocks systemd-analyze sockets; run this test outside that sandbox")
    assert result.returncode == 0, output
    commands = [
        line.partition("Command Line: ")[2]
        for line in output.splitlines()
        if "Command Line: " in line
    ]
    assert len(commands) == 1, output
    # The dump backslash-escapes dollar signs inside quotes (which shlex retains).
    # The doubled dollar is expanded later, when systemd executes the command.
    argv = [arg.replace(r"\$", "$").replace("$$", "$") for arg in shlex.split(commands[0])]
    assert argv == ["/usr/bin/true", "--user", "--config", str(source), "trigger", "monitor"]


def test_rendered_monitor_command_uses_global_job_options(tmp_path, valid_config):
    import shlex

    from vaultkeep.cli.parser import create_parser

    job = config(tmp_path, valid_config)
    job = replace(job, source_path=tmp_path / "job with spaces.yaml", params={"NETWORK": "testnet"})
    command = next(
        line.removeprefix("ExecStart=")
        for line in service.render_service(job).splitlines()
        if line.startswith("ExecStart=")
    )
    args = create_parser().parse_args(shlex.split(command)[1:])
    assert args.command == "trigger" and args.action == "monitor"
    assert args.config == job.source_path
    assert args.user_mode and args.param == ["NETWORK=testnet"]
