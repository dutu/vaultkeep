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


def config(tmp_path: Path, valid_config: dict):
    valid_config["trigger"] = {
        "enabled": True,
        "app": "electrum",
        "data_dir": "/home/user/.electrum",
        "matcher": {"entrypoint": {"kind": "script", "value": "/opt/electrum"}},
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
    assert f"vaultkeep --user trigger --config {job.source_path} monitor" in content
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

    from vaultkeep.scheduling import TimerManager, TimerPaths

    root_config = deepcopy(valid_config)
    root_config["schedule"]["enabled"] = True
    root_jobs = tmp_path / "etc/vaultkeep/jobs"
    root_jobs.mkdir(parents=True)
    root_job = root_jobs / "app.yaml"
    root_job.write_text(json.dumps(root_config))
    root_units = tmp_path / "etc/systemd/system"
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
    assert "vaultkeep --user trigger" in (user_units / monitor).read_text()
    assert ("systemctl", "enable", "--now", "vaultkeep@app.timer") in root_commands
    assert ("enable", "--now", monitor) in user_commands
    service.manage_service(triggered, "remove")
    assert {
        path: path.read_bytes() for path in root_units.rglob("*") if path.is_file()
    } == scheduled_files
    assert registry.read_bytes() == registry_before
