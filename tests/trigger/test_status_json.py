from __future__ import annotations

import fcntl
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.trigger.conftest import make_instance, make_request
from vaultkeep.cli import main
from vaultkeep.trigger.cli import _monitor_status
from vaultkeep.trigger.config import load_config
from vaultkeep.trigger.errors import BlockedError
from vaultkeep.trigger.models import AttemptOutcome, InstanceState, LaunchRecord, Receipt, utc_now
from vaultkeep.trigger.state import StateStore


@pytest.fixture
def status_args(tmp_path, valid_config, store, monkeypatch):
    config = tmp_path / "job.yaml"
    config.write_text(json.dumps(valid_config))
    monkeypatch.setattr("vaultkeep.trigger.cli.state_root", lambda _scope: store.root)
    monkeypatch.setattr(
        "vaultkeep.trigger.cli.monitor_lock_path", lambda _scope: tmp_path / "monitor.lock"
    )
    return ["--user", "--config", str(config), "trigger", "status", "--json"]


@pytest.mark.parametrize(
    ("pending", "active", "expected"),
    [
        (False, False, "idle"),
        (True, False, "pending"),
        (False, True, "active"),
        (True, True, "active"),
    ],
)
def test_json_activity(status_args, store, capsys, pending, active, expected):
    request = make_request(make_instance(instance_id="app"))
    state = InstanceState(
        instance_id="app",
        pending_request=replace(request, attempt_id="") if pending else None,
        active_attempt_id=request.attempt_id if active else None,
    )
    store.write_instance(state)
    if active:
        store.write_request(request)
        store.write_launch(LaunchRecord(request.attempt_id, "worker.service", utc_now()))
    assert main(status_args) == 0
    first = capsys.readouterr()
    assert first.err == ""
    assert json.loads(first.out) == {
        "schema_version": 1,
        "job_id": "app",
        "backup_activity": expected,
        "pending_request_id": request.request_id if pending else None,
        "active_attempt_id": request.attempt_id if active else None,
        "monitor_status": "stopped",
    }
    assert len(first.out.splitlines()) == 1
    assert main(status_args) == 0
    assert capsys.readouterr().out == first.out


def test_missing_state_is_unknown_and_creates_no_files(status_args, tmp_path, monkeypatch, capsys):
    root = tmp_path / "absent"
    monkeypatch.setattr("vaultkeep.trigger.cli.state_root", lambda _scope: root)
    assert main(status_args) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["backup_activity"] == "unknown"
    assert output.err == ""
    assert not root.exists()
    assert not (tmp_path / "monitor.lock").exists()


@pytest.mark.parametrize(
    "content",
    [
        b"{corrupt",
        b"[]",
        b'{"schema_version": 999, "instance_id": "app"}',
        b'{"schema_version": 1, "instance_id": "app", "active_attempt_id": 4}',
        b"\xff",
    ],
)
def test_corrupt_state_is_unknown_with_exit_2(status_args, store, capsys, content):
    store.instance_path("app").write_bytes(content)
    assert main(status_args) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["backup_activity"] == "unknown"
    assert "StateError:" in output.err
    assert store.instance_path("app").read_bytes() == content


def test_unreadable_state_is_unknown_with_exit_2(status_args, store, monkeypatch, capsys):
    store.write_instance(InstanceState(instance_id="app"))
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if path == store.instance_path("app"):
            raise PermissionError("state is unreadable")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert main(status_args) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["backup_activity"] == "unknown"
    assert "state is unreadable" in output.err


def test_unreadable_attempt_directory_is_unknown(status_args, store, monkeypatch, capsys):
    store.write_instance(InstanceState(instance_id="app"))

    def unreadable(_self):
        raise PermissionError("attempts are unreadable")

    monkeypatch.setattr(StateStore, "list_attempt_ids", unreadable)
    assert main(status_args) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["backup_activity"] == "unknown"
    assert "attempts are unreadable" in output.err


def test_invalid_attempt_directory_is_unknown(status_args, store, capsys):
    store.write_instance(InstanceState(instance_id="app"))
    store.attempts_dir.rmdir()
    store.attempts_dir.write_text("not a directory")
    assert main(status_args) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["backup_activity"] == "unknown"
    assert "NotADirectoryError:" in output.err


@pytest.mark.parametrize("active", [False, True])
def test_block_does_not_prove_idle(status_args, store, capsys, active):
    store.write_instance(
        InstanceState(instance_id="app", active_attempt_id="attempt" if active else None)
    )
    store.write_blocked("app", "state requires review")
    assert main(status_args) == 0
    assert json.loads(capsys.readouterr().out)["backup_activity"] == (
        "active" if active else "unknown"
    )


def test_corrupt_block_is_a_state_error(status_args, store, capsys):
    store.write_instance(InstanceState(instance_id="app"))
    store.write_blocked("app", "review")
    store.blocked_path("app").write_text("{corrupt")
    assert main(status_args) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["backup_activity"] == "unknown"
    assert "StateError:" in output.err


@pytest.mark.parametrize("with_state", [False, True])
def test_unassociated_launch_is_active(status_args, store, capsys, with_state):
    if with_state:
        store.write_instance(InstanceState(instance_id="app"))
    request = make_request(make_instance(instance_id="app"))
    store.write_request(request)
    store.write_launch(LaunchRecord(request.attempt_id, "worker.service", utc_now()))
    assert main(status_args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["backup_activity"] == "active"
    assert output["active_attempt_id"] == request.attempt_id


@pytest.mark.parametrize("summarized", [False, True])
def test_obsolete_and_completed_attempts_do_not_imply_activity(
    status_args, store, capsys, summarized
):
    request = make_request(make_instance(instance_id="app"))
    state = InstanceState(instance_id="app")
    store.write_request(request)
    if summarized:
        store.write_launch(LaunchRecord(request.attempt_id, "worker.service", utc_now()))
        state.attempt_summaries.append(
            {
                "attempt_id": request.attempt_id,
                "request_id": request.request_id,
                "outcome": AttemptOutcome.CREATED.value,
            }
        )
    store.write_instance(state)
    assert main(status_args) == 0
    assert json.loads(capsys.readouterr().out)["backup_activity"] == "idle"


def test_receipt_does_not_clear_recorded_active_attempt(status_args, store, capsys):
    request = make_request(make_instance(instance_id="app"))
    store.write_request(request)
    store.write_receipt(
        Receipt(
            attempt_id=request.attempt_id,
            request_id=request.request_id,
            instance_id="app",
            outcome=AttemptOutcome.CREATED,
            started_at=utc_now(),
            ended_at=utc_now(),
            duration_seconds=1.0,
        )
    )
    store.write_instance(
        InstanceState(
            instance_id="app",
            active_attempt_id=request.attempt_id,
            attempt_summaries=[
                {
                    "attempt_id": request.attempt_id,
                    "request_id": request.request_id,
                    "outcome": AttemptOutcome.CREATED.value,
                }
            ],
        )
    )
    assert main(status_args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["backup_activity"] == "active"
    assert report["active_attempt_id"] == request.attempt_id
    assert report["monitor_status"] == "stopped"


def test_unreadable_attempt_ownership_prevents_idle(status_args, store, capsys):
    store.write_instance(InstanceState(instance_id="app"))
    request = make_request(make_instance(instance_id="app"))
    store.write_request(request)
    store.request_path(request.attempt_id).write_text("{corrupt")
    assert main(status_args) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["backup_activity"] == "unknown"
    assert "StateError:" in output.err


def test_instance_selection_is_preserved(status_args, store, capsys):
    store.write_instance(InstanceState(instance_id="app"))
    store.write_instance(InstanceState(instance_id="legacy", active_attempt_id="legacy-attempt"))
    assert main([*status_args, "legacy"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["job_id"] == "app"
    assert output["backup_activity"] == "active"
    assert output["active_attempt_id"] == "legacy-attempt"
    assert main(status_args) == 0
    assert json.loads(capsys.readouterr().out)["backup_activity"] == "idle"
    assert main([*status_args, "missing"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "unknown instance: missing" in output.err


def test_json_accepts_config_before_trigger(status_args, store, capsys):
    store.write_instance(InstanceState(instance_id="app"))
    assert main(["--user", "--config", status_args[2], "trigger", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["backup_activity"] == "idle"


def test_parameter_selection_uses_the_correct_job_namespace(
    status_args, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("vaultkeep.trigger.cli.state_root", lambda scope: tmp_path / scope)
    for parameter, activity in (("main", "active"), ("test", "idle")):
        config = load_config(status_args[2], runtime_params={"network": parameter})
        store = StateStore(tmp_path / config.scope)
        store.write_instance(
            InstanceState(
                instance_id="app",
                active_attempt_id="attempt" if activity == "active" else None,
            )
        )
        assert main([*status_args[:3], "--param", f"network={parameter}", *status_args[3:]]) == 0
        assert json.loads(capsys.readouterr().out)["backup_activity"] == activity


def test_instance_selection_cannot_read_outside_namespace(status_args, store, monkeypatch, capsys):
    def unexpected(*_args):
        raise AssertionError("invalid selection must be rejected before reading instance state")

    monkeypatch.setattr(StateStore, "read_instance", unexpected)
    assert main([*status_args, "../outside"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "unknown instance:" in output.err


def test_invalid_configuration_has_nonzero_exit_and_no_stdout(tmp_path, capsys):
    assert (
        main(["--user", "--config", str(tmp_path / "missing"), "trigger", "status", "--json"]) == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "ConfigurationError:" in output.err


@pytest.mark.parametrize("activity", ["idle", "pending", "active"])
def test_unavailable_monitor_information_is_independent(
    status_args, store, monkeypatch, capsys, activity
):
    request = make_request(make_instance(instance_id="app"))
    store.write_instance(
        InstanceState(
            instance_id="app",
            pending_request=request if activity == "pending" else None,
            active_attempt_id=request.attempt_id if activity == "active" else None,
        )
    )

    def unavailable(_scope):
        raise BlockedError("no runtime directory")

    monkeypatch.setattr("vaultkeep.trigger.cli.monitor_lock_path", unavailable)
    assert main(status_args) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["backup_activity"] == activity
    assert report["monitor_status"] == "unknown"
    assert output.err == ""


def test_monitor_probe_uses_shared_lock_only(tmp_path, monkeypatch):
    lock = tmp_path / "monitor.lock"
    lock.write_text("pid\n")
    monkeypatch.setattr("vaultkeep.trigger.cli.monitor_lock_path", lambda _scope: lock)
    original_flock = fcntl.flock

    def shared_only(fd, operation):
        assert operation == fcntl.LOCK_SH | fcntl.LOCK_NB
        return original_flock(fd, operation)

    with lock.open("r") as owner:
        original_flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr("vaultkeep.trigger.cli.fcntl.flock", shared_only)
        assert _monitor_status(None) == "running"
        original_flock(owner, fcntl.LOCK_UN)
        assert _monitor_status(None) == "stopped"
    assert lock.read_text() == "pid\n"


@pytest.mark.parametrize("operation", ["open", "flock"])
def test_unreadable_monitor_information_is_unknown(tmp_path, monkeypatch, operation):
    lock = tmp_path / "monitor.lock"
    lock.touch()
    monkeypatch.setattr("vaultkeep.trigger.cli.monitor_lock_path", lambda _scope: lock)

    def denied(*_args):
        raise PermissionError("monitor information unavailable")

    monkeypatch.setattr(
        f"vaultkeep.trigger.cli.{'os.open' if operation == 'open' else 'fcntl.flock'}", denied
    )
    assert _monitor_status(None) == "unknown"


def test_json_status_is_read_only(status_args, store, tmp_path, monkeypatch, capsys):
    request = make_request(make_instance(instance_id="app"))
    store.write_instance(InstanceState(instance_id="app", active_attempt_id=request.attempt_id))
    store.write_request(request)
    store.write_launch(LaunchRecord(request.attempt_id, "worker.service", utc_now()))
    (tmp_path / "monitor.lock").write_text("old pid\n")

    def snapshot():
        return {
            path: (
                path.stat().st_mode,
                path.stat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
            for path in tmp_path.rglob("*")
        }

    before = snapshot()

    def unexpected(*_args, **_kwargs):
        raise AssertionError("status must only inspect existing evidence")

    for name in (
        "ensure",
        "write_instance",
        "write_request",
        "write_launch",
        "write_receipt",
        "write_blocked",
        "remove_attempt",
    ):
        monkeypatch.setattr(StateStore, name, unexpected)
    for name in ("manage_service", "RealSystemdController", "Monitor", "exec_worker"):
        monkeypatch.setattr(f"vaultkeep.trigger.cli.{name}", unexpected)
    assert main(status_args) == 0
    assert json.loads(capsys.readouterr().out)["backup_activity"] == "active"
    assert snapshot() == before
