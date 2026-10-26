"""Real process termination and rendered timer commands use the common engine."""

from __future__ import annotations

import json
import os
import pwd
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from tests.trigger.conftest import FakeController, FakeProvider, make_request
from tests.trigger.test_monitor import _running_proc
from vaultkeep.backup.paths import user_workflow_paths
from vaultkeep.backup.status import backup_status
from vaultkeep.config import JobConfig
from vaultkeep.notifications.notifier import build_notifier
from vaultkeep.schedule import TimerManager
from vaultkeep.state.identity import job_identity_hash
from vaultkeep.trigger.config import load_config as load_trigger_config
from vaultkeep.trigger.executor import exec_worker
from vaultkeep.trigger.monitor import Monitor, unit_name_for
from vaultkeep.trigger.state import StateStore
from vaultkeep.trigger.systemd import UnitState


@pytest.fixture
def job(tmp_path, valid_config, monkeypatch):
    for key, name in (
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_RUNTIME_DIR", "runtime"),
    ):
        monkeypatch.setenv(key, str(tmp_path / name))
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_text("backup contents")
    destination = tmp_path / "backups"
    destination.mkdir()
    valid_config["sources"][0]["path"] = str(source)
    valid_config["destination"].update(root=str(destination))
    valid_config["schedule"] = {"enabled": False}
    path = tmp_path / "app.yaml"
    path.write_text(json.dumps(valid_config))
    return path


@pytest.mark.parametrize(
    "signum,outcome", [(signal.SIGTERM, "INTERRUPTED"), (signal.SIGKILL, "UNKNOWN")]
)
def test_terminated_execution_is_never_left_running(job, signum, outcome):
    ready = job.parent / "ready"
    script = """
import sys, time
from pathlib import Path
from vaultkeep.backup import workflow
from vaultkeep.cli import main
def paused_build(request, **kwargs):
    kwargs['progress']('Verifying archive')
    Path(sys.argv[2]).touch()
    time.sleep(30)
workflow.build_archive = paused_build
raise SystemExit(main(['--user', '--config', sys.argv[1], 'run']))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(job), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    identity = job_identity_hash(job, "app")
    paths = user_workflow_paths()
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), process.communicate(timeout=1)
        running = backup_status(paths, "app", identity)
        assert running["activity"] == "RUNNING"
        assert running["current"]["phase"] == "Verifying archive"
        process.send_signal(signum)
        process.communicate(timeout=10)
        final = backup_status(paths, "app", identity)
        assert final["activity"] == "IDLE"
        assert final["previous"]["result"] == outcome
        if outcome == "INTERRUPTED":
            assert process.returncode == 143
            assert not list((job.parent / "backups").iterdir())
        else:
            assert final["previous"]["completed_at"] is None
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_rendered_schedule_uses_the_same_run_command_and_result_history(job):
    config = JobConfig.model_validate(json.loads(job.read_text()))
    manager = TimerManager(user_mode=True)
    content = manager._service_content(job, config.job.id, {})
    command = shlex.split(
        next(
            line.removeprefix("ExecStart=")
            for line in content.splitlines()
            if line.startswith("ExecStart=")
        )
    )
    assert command[1:] == ["--user", "--config", str(job), "run"]
    # Use this interpreter's CLI instead of the deployment-specific executable.
    # Invalid runtime storage makes the outcome check independent of archive tools.
    (job.parent / "backups").rmdir()
    scheduled = subprocess.run(
        [sys.executable, "-m", "vaultkeep", *command[1:]],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert scheduled.returncode == 5
    result = backup_status(user_workflow_paths(), "app", job_identity_hash(job, "app"))
    assert result["previous"]["result"] == "FAILED"
    assert result["previous"]["exit_code"] == 5
    assert "Destination root" in result["previous"]["reason"]


@pytest.fixture
def notification_log(job):
    log = job.parent / "notifications.jsonl"
    command = job.parent / "capture-notification"
    command.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"with open({str(log)!r}, 'a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    command.chmod(0o700)
    document = json.loads(job.read_text())
    document["notifications"] = {
        "command": str(command),
        "timeout_ms": 500,
        "backup": {"enabled": True},
        "trigger": {"enabled": False},
    }
    job.write_text(json.dumps(document))
    return log


@pytest.mark.parametrize("backup", [False, True])
@pytest.mark.parametrize("trigger", [False, True])
def test_triggered_backup_categories_are_independent(job, notification_log, backup, trigger):
    document = json.loads(job.read_text())
    document["notifications"]["backup"]["enabled"] = backup
    document["notifications"]["trigger"]["enabled"] = trigger
    document["application"] = {
        "name": "electrum",
        "data_dir": "/home/user/.electrum",
        "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/app/run_app"}]},
        "user": pwd.getpwuid(os.geteuid()).pw_name,
    }
    document["trigger"] = {"enabled": True, "confirm_seconds": 1.0}
    job.write_text(json.dumps(document))
    (job.parent / "backups").rmdir()
    config = load_trigger_config(job)
    store = StateStore(job.parent / "trigger")
    provider, controller = FakeProvider([_running_proc()]), FakeController()
    notifier = build_notifier(config.notifications)
    clock = [0.0]
    monitor = Monitor(
        config,
        store=store,
        provider=provider,
        controller=controller,
        notifier=notifier,
        clock=lambda: clock[0],
    )
    try:
        monitor.run_once()
        provider.processes = []
        clock[0] = 1.0
        monitor.run_once()
        clock[0] = 2.0
        monitor.run_once()
        state = store.read_instance("app")
        assert state.active_attempt_id
        result = exec_worker(state.active_attempt_id, store=store, provider=provider)
        assert result.outcome.value == "backup_failed"
        controller.states[unit_name_for(state.active_attempt_id, config.scope)] = UnitState(
            "loaded", "inactive", "dead"
        )
        monitor.run_once()
        monitor.run_once()
        assert store.read_instance("app").active_attempt_id is None
    finally:
        notifier.close()
    messages = (
        [json.loads(line) for line in notification_log.read_text().splitlines()]
        if notification_log.exists()
        else []
    )
    backup_titles = [title for title, _ in messages if title.startswith("VaultKeep: Backup")]
    trigger_titles = [title for title, _ in messages if title.startswith("VaultKeep Trigger:")]
    assert bool(backup_titles) is backup
    assert bool(trigger_titles) is trigger
    if backup:
        assert backup_titles[0] == "VaultKeep: Backup started"
        assert backup_titles[-1] == "VaultKeep: Backup completed"
        assert backup_titles.count("VaultKeep: Backup completed") == 1
    if trigger:
        assert "VaultKeep Trigger: Dispatch requested" in trigger_titles
        assert not any("completed" in title or "finished" in title for title in trigger_titles)


@pytest.mark.parametrize("initiation", ["manual", "scheduled", "triggered"])
def test_all_initiators_deliver_start_and_diagnostic_failure_before_exit(
    job, notification_log, initiation
):
    # A runtime failure after lock acquisition exercises the real common engine
    # without depending on archive tools or a graphical session.
    (job.parent / "backups").rmdir()
    if initiation == "triggered":
        executable = job.parent / "vaultkeep"
        executable.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m vaultkeep "$@"\n')
        executable.chmod(0o700)
        document = json.loads(job.read_text())
        document["application"] = {
            "name": "electrum",
            "user": pwd.getpwuid(os.geteuid()).pw_name,
            "data_dir": str(job.parent / "source"),
            "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/app/run_app"}]},
        }
        document["trigger"] = {"enabled": True}
        job.write_text(json.dumps(document))
        request = replace(
            make_request(load_trigger_config(job).instances[0]),
            instance_id="app",
            job_config=str(job),
            vaultkeep=str(executable),
            environment=dict(os.environ),
        )
        store = StateStore(job.parent / "trigger")
        store.write_request(request)
        result = exec_worker(request.attempt_id, store=store, provider=FakeProvider([]))
        assert result.outcome.value == "backup_failed"
    else:
        arguments = ["--user", "--config", str(job), "run"]
        if initiation == "scheduled":
            config = JobConfig.model_validate(json.loads(job.read_text()))
            service = TimerManager(user_mode=True)._service_content(job, config.job.id, {})
            arguments = shlex.split(
                next(
                    line.removeprefix("ExecStart=")
                    for line in service.splitlines()
                    if line.startswith("ExecStart=")
                )
            )[1:]
        result = subprocess.run(
            [sys.executable, "-m", "vaultkeep", *arguments],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 5
    messages = [json.loads(line) for line in notification_log.read_text().splitlines()]
    assert messages[0][0] == "VaultKeep: Backup started"
    assert messages[-1][0] == "VaultKeep: Backup completed"
    assert "failed: Destination root" in messages[-1][1]
    assert sum(title == "VaultKeep: Backup started" for title, _ in messages) == 1
    assert sum(title == "VaultKeep: Backup completed" for title, _ in messages) == 1
    assert not any(title.startswith("VaultKeep Trigger:") for title, _ in messages)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGKILL])
def test_termination_notifications_follow_available_completion_evidence(
    job, notification_log, signum
):
    ready = job.parent / "ready"
    script = """
import sys, time
from pathlib import Path
from vaultkeep.backup import workflow
from vaultkeep.cli import main
def pause(*args, **kwargs):
    Path(sys.argv[2]).touch()
    time.sleep(30)
workflow._run_single_backup = pause
raise SystemExit(main(['--user', '--config', sys.argv[1], 'run']))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(job), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if ready.exists() and notification_log.exists():
                break
            assert process.poll() is None, process.communicate(timeout=1)
            time.sleep(0.01)
        assert ready.exists() and notification_log.exists()
        process.send_signal(signum)
        process.communicate(timeout=10)
        messages = [json.loads(line) for line in notification_log.read_text().splitlines()]
        completed = [body for title, body in messages if title == "VaultKeep: Backup completed"]
        if signum == signal.SIGTERM:
            assert len(completed) == 1 and "interrupted" in completed[0]
        else:
            assert not completed
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
