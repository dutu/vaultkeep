"""Execution notifications use lifecycle facts without depending on status telemetry."""

from __future__ import annotations

import signal
import time

import pytest

from tests.trigger.conftest import RecordingNotifier
from vaultkeep.backup.execution import Execution
from vaultkeep.backup.notifications import BackupNotifications
from vaultkeep.backup.paths import WorkflowPaths
from vaultkeep.backup.records import Attempt, ExecutionHistory, read_history
from vaultkeep.backup.results import BackupInterrupted
from vaultkeep.backup.workflow import CommandResult, run_backup
from vaultkeep.errors import DestinationError, LockError, StateError
from vaultkeep.notifications.notifier import NotificationConfig
from vaultkeep.state.atomic import write_json


@pytest.fixture
def recorder(monkeypatch):
    notifier = RecordingNotifier()
    notifier.configs = []

    def build(config):
        notifier.configs.append(config)
        return notifier

    monkeypatch.setattr("vaultkeep.backup.notifications.build_notifier", build)
    return notifier


@pytest.fixture
def execution(tmp_path, recorder):
    return Execution(
        WorkflowPaths(tmp_path / "state", tmp_path / "cache", tmp_path / "locks"),
        "app",
        "a" * 16,
        notifications=NotificationConfig(enabled=True),
    )


@pytest.mark.parametrize(
    "outcome, text",
    [
        ("created", "new archive created"),
        ("unchanged", "sources unchanged"),
        ("failed", "failed: file could not be read"),
        (None, "outcome unknown"),
    ],
)
def test_execution_messages_follow_the_authoritative_result(execution, recorder, outcome, text):
    assert not recorder.calls
    with execution:
        assert recorder.calls[0] == ("VaultKeep: Backup started", "job app: execution started")
        execution.report("Verifying archive")
        if outcome:
            execution.complete(
                outcome, errors=["file could not be read"] if outcome == "failed" else []
            )
    assert recorder.calls[-1][0] == "VaultKeep: Backup completed"
    assert text in recorder.calls[-1][1]
    assert "(local time)" in recorder.calls[-1][1]
    assert all(key == f"backup:{execution.execution_id}" for key, _ in recorder.options)
    assert recorder.options[-1][1]
    history = read_history(execution.directory / "execution.json", "app", "a" * 16)
    assert history.current is None


@pytest.mark.parametrize("result", ["SUCCESS", "UNCHANGED", "FAILED", "INTERRUPTED", "UNKNOWN"])
def test_result_time_uses_actual_completion_in_local_timezone(recorder, monkeypatch, result):
    notifications = BackupNotifications(
        NotificationConfig(enabled=True),
        "app",
        "a" * 32,
        per_file=False,
    )
    attempt = Attempt(
        execution_id="a" * 32,
        started_at="2026-10-22T09:00:00Z",
        pid=123,
        result=result,
        completed_at="2026-10-22T10:00:00Z",
        duration_seconds=3600.0,
        exit_code=7 if result in ("FAILED", "INTERRUPTED") else 0,
    )
    with monkeypatch.context() as context:
        context.setenv("TZ", "EET-2EEST,M3.5.0/3,M10.5.0/4")
        time.tzset()
        try:
            notifications.completed(attempt)
            assert recorder.calls[-1][1].endswith("\n2026-10-22 13:00:00 EEST (local time)")
        finally:
            context.undo()
            time.tzset()


@pytest.mark.parametrize(
    "error, result, reason",
    [
        (
            DestinationError("Destination root unavailable"),
            "FAILED",
            "Destination root unavailable",
        ),
        (BackupInterrupted(signal.SIGTERM), "INTERRUPTED", "signal 15"),
        (KeyboardInterrupt(), "INTERRUPTED", "KeyboardInterrupt"),
        (SystemExit(1), "INTERRUPTED", "interrupted"),
    ],
)
def test_failure_and_interruption_preserve_results_and_diagnostics(
    execution, recorder, error, result, reason
):
    with pytest.raises(type(error)), execution:
        raise error
    assert reason in recorder.calls[-1][1]
    history = read_history(execution.directory / "execution.json", "app", "a" * 16)
    assert history.previous.result == result


def test_start_requires_lock_and_shutdown_drains_after_releasing_it(
    execution, recorder, monkeypatch
):
    execution.lock.acquire()
    try:
        with pytest.raises(LockError), execution:
            pytest.fail("contended execution must not enter")
        assert not recorder.calls
    finally:
        execution.lock.release()
    closed = []

    def close():
        execution.lock.acquire()
        execution.lock.release()
        closed.append(True)

    monkeypatch.setattr(recorder, "close", close)
    with execution:
        execution.complete("created")
    assert closed == [True]


def test_phase_changes_and_elapsed_refresh_are_throttled(recorder, monkeypatch):
    seconds = [0.0]
    monkeypatch.setattr("vaultkeep.backup.notifications.monotonic", lambda: seconds[0])
    notifications = BackupNotifications(
        NotificationConfig(enabled=True), "app", "exec", per_file=False
    )
    notifications.started()
    notifications.report("Checking existing backups")
    for value in (0, 9, 10, 19, 20):
        seconds[0] = value
        notifications.heartbeat()
    assert [body for title, body in recorder.calls if "running" in title] == [
        "job app: Checking existing backups",
        "job app: Checking existing backups (10s)",
        "job app: Checking existing backups (20s)",
    ]
    seconds[0] = 25
    notifications.report("Verifying archive")
    seconds[0] = 34
    notifications.heartbeat()
    assert recorder.calls[-1][1] == "job app: Verifying archive"
    seconds[0] = 35
    notifications.heartbeat()
    assert recorder.calls[-1][1] == "job app: Verifying archive (10s)"


def test_per_file_updates_share_one_stream_and_cap_phase_changes(recorder, monkeypatch):
    seconds = [0.0]
    monkeypatch.setattr("vaultkeep.backup.notifications.monotonic", lambda: seconds[0])
    notifications = BackupNotifications(
        NotificationConfig(enabled=True), "app", "exec", per_file=True
    )
    notifications.started()
    for index in range(100):
        notifications.report(f"Backing up file {index}")
        notifications.report("Creating and compressing archive")
        notifications.report("Verifying archive")
    assert len(recorder.calls) == 1
    seconds[0] = 10
    notifications.heartbeat()
    assert recorder.calls[-1][1] == "job app: Verifying archive (10s)"
    assert {key for key, _ in recorder.options} == {"backup:exec"}


@pytest.mark.parametrize("fault", ["delivery", "close", "evidence"])
def test_optional_reporting_failures_cannot_change_execution_result(
    execution, recorder, monkeypatch, fault
):
    def fail(*_args, **_kwargs):
        raise StateError("optional reporting unavailable")

    if fault == "evidence":
        monkeypatch.setattr("vaultkeep.backup.execution.write_json", fail)
    else:
        monkeypatch.setattr(recorder, "notify" if fault == "delivery" else "close", fail)
    with execution:
        execution.report("Creating and compressing archive")
        execution.complete("created")
    assert execution.history.previous.result == "SUCCESS"
    if fault == "evidence":
        assert "new archive created" in recorder.calls[-1][1]


def test_unfinished_prior_execution_is_reported_as_unknown_on_recovery(execution, recorder):
    previous = Attempt(
        execution_id="b" * 32,
        started_at="2020-01-01T00:00:00Z",
        pid=99999,
    )
    write_json(
        execution.directory / "execution.json",
        ExecutionHistory(
            schema_version=1,
            job_id="app",
            identity_hash="a" * 16,
            current=previous,
            previous=None,
        ).model_dump(mode="json"),
    )
    with execution:
        execution.complete("unchanged")
    assert "outcome unknown: Backup completion could not be verified" in recorder.calls[0][1]
    assert recorder.options[0] == (f"backup:{previous.execution_id}", True)
    assert recorder.calls[1][0] == "VaultKeep: Backup started"
    assert "sources unchanged" in recorder.calls[-1][1]


@pytest.mark.parametrize("backup", [False, True])
@pytest.mark.parametrize("trigger", [False, True])
def test_common_workflow_uses_only_backup_notification_setting(
    tmp_path, valid_config, recorder, monkeypatch, backup, trigger
):
    import json

    from vaultkeep.trigger.config import load_config as trigger_config

    valid_config["desktop_notifications"] = {
        "command": "/custom/notify",
        "timeout_ms": 250,
        "backup": {"enabled": backup},
        "trigger": {"enabled": trigger},
    }
    valid_config["destination"]["root"] = str(tmp_path)
    path = tmp_path / "app.yaml"
    path.write_text(json.dumps(valid_config))
    monkeypatch.setattr("vaultkeep.validation.validate_semantics", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "vaultkeep.backup.workflow._run_single_backup",
        lambda *_a, **_k: CommandResult("run", "created"),
    )
    paths = WorkflowPaths(tmp_path / "state", tmp_path / "cache", tmp_path / "locks")
    assert run_backup(path, paths=paths, user_mode=True).result == "created"
    assert bool(recorder.calls) is backup
    assert recorder.configs == [NotificationConfig(backup, "/custom/notify", 250)]
    assert trigger_config(path).notifications == NotificationConfig(trigger, "/custom/notify", 250)
