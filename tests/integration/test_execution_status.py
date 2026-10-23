"""Real process termination and rendered timer commands use the common engine."""

from __future__ import annotations

import json
import shlex
import signal
import subprocess
import sys
import time

import pytest

from vaultkeep.backup.paths import user_workflow_paths
from vaultkeep.backup.status import backup_status
from vaultkeep.config import JobConfig
from vaultkeep.schedule import TimerManager
from vaultkeep.state.identity import job_identity_hash


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
    valid_config["destination"].update(root=str(destination), mount_point=None)
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
        assert final["activity"] != "RUNNING"
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
