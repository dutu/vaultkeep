from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from vaultkeep.cli import main
from vaultkeep.trigger.config import load_config
from vaultkeep.trigger.models import AttemptOutcome, InstanceState, LaunchRecord, Receipt, utc_now
from vaultkeep.trigger.monitor import unit_name_for
from vaultkeep.trigger.systemd import UnitState
from vaultkeep.version import installed_version

from .conftest import FakeProvider, FakeRunner, child_result, make_instance, make_request


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
        main(["--user", "trigger", "status"])


def test_status_without_state(tmp_path: Path, valid_config: dict, monkeypatch, capsys) -> None:
    monkeypatch.setattr("vaultkeep.trigger.cli.state_root", lambda _scope: tmp_path / "state")
    monkeypatch.setattr(
        "vaultkeep.trigger.cli._monitor_runtime_status", lambda _scope: "not running"
    )
    assert main(["--user", "--config", str(job(tmp_path, valid_config)), "trigger", "status"]) == 0
    assert "no recorded instances" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def test_status_corrupt_state_exits_2(tmp_path: Path, valid_config: dict, monkeypatch) -> None:
    root = tmp_path / "state"
    instances = root / "instances"
    instances.mkdir(parents=True)
    (instances / "app.json").write_text("{not json")
    monkeypatch.setattr("vaultkeep.trigger.cli.state_root", lambda _scope: root)
    assert main(["--user", "--config", str(job(tmp_path, valid_config)), "trigger", "status"]) == 2


@pytest.mark.parametrize("extra", [[], ["--runtime"]])
def test_removed_check_config_is_rejected(monkeypatch, capsys, extra) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("removed command must be rejected before loading configuration")

    monkeypatch.setattr("vaultkeep.trigger.cli._load", unexpected)
    with pytest.raises(SystemExit) as error:
        main(["--user", "--config", "/missing/job.yaml", "trigger", "check-config", *extra])
    assert error.value.code == 2
    assert "invalid choice: 'check-config'" in capsys.readouterr().err


def test_config_can_precede_trigger(tmp_path: Path, valid_config: dict, monkeypatch) -> None:
    monkeypatch.setattr("vaultkeep.trigger.cli.state_root", lambda _scope: tmp_path / "state")
    assert main(["--user", "--config", str(job(tmp_path, valid_config)), "trigger", "status"]) == 0


def test_missing_config_exits_2(tmp_path: Path) -> None:
    assert main(["--user", "--config", str(tmp_path / "missing.yaml"), "trigger", "status"]) == 2


def test_monitor_refuses_disabled_trigger(tmp_path: Path, valid_config: dict) -> None:
    assert main(["--user", "--config", str(job(tmp_path, valid_config)), "trigger", "monitor"]) == 2


@pytest.mark.parametrize("identifier", ["../../evil", "a" * 16 + "_../../evil", "a" * 16 + "_bad"])
def test_worker_rejects_invalid_namespace_or_attempt(identifier: str) -> None:
    assert main(["--user", "trigger", "exec-worker", identifier]) == 2


@pytest.mark.parametrize(
    "command",
    ["monitor", "status", "install", "update", "disable", "remove", "exec-worker"],
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
    ["monitor", "status", "install", "update", "disable", "remove", "exec-worker"],
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


@pytest.mark.parametrize(
    "runtime, status",
    [("running (lock held by pid 123)", "RUNNING"), ("not running (lock is free)", "STOPPED")],
)
def test_human_status_separates_runtime_from_configuration(
    tmp_path, valid_config, monkeypatch, capsys, runtime, status
):
    valid_config["logging"]["level"] = "error"
    monkeypatch.setattr("vaultkeep.trigger.cli.state_root", lambda _scope: tmp_path / "state")
    monkeypatch.setattr("vaultkeep.trigger.cli._monitor_runtime_status", lambda _scope: runtime)
    assert main(["--user", "--config", str(job(tmp_path, valid_config)), "trigger", "status"]) == 0
    output = capsys.readouterr()
    assert output.out.startswith("Vaultkeep — Trigger status\n\n")
    assert "Trigger:  DISABLED" in output.out
    assert f"Monitor:  {status}" in output.out
    assert "no recorded instances" in output.out
    assert "vaultkeep-trigger@" not in output.out
    assert output.err == ""


@pytest.fixture
def human_status_args(tmp_path, valid_config, store, monkeypatch):
    valid_config["logging"]["level"] = "error"
    config = job(tmp_path, valid_config)
    monkeypatch.setattr("vaultkeep.trigger.cli.state_root", lambda _scope: store.root)
    monkeypatch.setattr(
        "vaultkeep.trigger.cli._monitor_runtime_status", lambda _scope: "not running"
    )
    monkeypatch.setattr(
        "vaultkeep.trigger.cli.RealSystemdController.unit_state", lambda _self, _unit: None
    )
    return ["--user", "--config", str(config), "trigger", "status"]


@pytest.mark.parametrize(
    ("pending", "active", "expected"),
    [
        (False, False, "IDLE"),
        (True, False, "PENDING"),
        (False, True, "ACTIVE"),
        (True, True, "ACTIVE"),
    ],
)
def test_human_backup_activity(human_status_args, store, capsys, pending, active, expected):
    request = make_request(make_instance(instance_id="app"))
    store.write_instance(
        InstanceState(
            instance_id="app",
            pending_request=replace(request, attempt_id="") if pending else None,
            active_attempt_id=request.attempt_id if active else None,
        )
    )
    assert main(human_status_args) == 0
    output = " ".join(capsys.readouterr().out.split())
    assert f"Backup activity {expected}" in output
    assert "Monitor: STOPPED" in output
    if active:
        assert "Worker UNKNOWN (systemd unavailable)" in output
        assert request.attempt_id in output


@pytest.mark.parametrize(
    ("worker", "expected"),
    [
        (
            UnitState("loaded", "activating", "start", job_pending=True),
            "RUNNING (activating/start)",
        ),
        (UnitState("loaded", "active", "running"), "RUNNING (active/running)"),
        (UnitState("loaded", "inactive", "dead", job_pending=True), "QUEUED (inactive/dead)"),
        (
            UnitState("loaded", "activating", "start-pre", job_pending=True),
            "QUEUED (activating/start-pre)",
        ),
        (
            UnitState("loaded", "deactivating", "stop", job_pending=True),
            "STOPPING (deactivating/stop)",
        ),
        (
            UnitState("loaded", "inactive", "dead"),
            "STOPPED (inactive/dead; awaiting reconciliation)",
        ),
        (
            UnitState("loaded", "failed", "failed"),
            "STOPPED (failed/failed; awaiting reconciliation)",
        ),
        (
            UnitState("not-found", "inactive", "dead"),
            "STOPPED (inactive/dead; awaiting reconciliation)",
        ),
        (UnitState("loaded", "active", "exited"), "UNKNOWN (active/exited)"),
        (None, "UNKNOWN (systemd unavailable)"),
    ],
)
def test_human_status_probes_exact_worker(
    human_status_args, store, monkeypatch, capsys, worker, expected
):
    request = make_request(make_instance(instance_id="app"))
    store.write_instance(InstanceState(instance_id="app", active_attempt_id=request.attempt_id))
    probes = []

    def probe(_self, unit):
        probes.append(unit)
        return worker

    monkeypatch.setattr("vaultkeep.trigger.cli.RealSystemdController.unit_state", probe)
    assert main(human_status_args) == 0
    output = " ".join(capsys.readouterr().out.split())
    unit = unit_name_for(request.attempt_id, load_config(Path(human_status_args[2])).scope)
    assert probes == [unit]
    assert f"Worker {expected}" in output
    assert f"Worker service {unit}" in output
    assert "Backup activity ACTIVE" in output


@pytest.mark.parametrize("with_state", [False, True])
def test_human_status_finds_unassociated_worker(human_status_args, store, capsys, with_state):
    if with_state:
        store.write_instance(InstanceState(instance_id="app"))
    request = make_request(make_instance(instance_id="app"))
    store.write_request(request)
    store.write_launch(LaunchRecord(request.attempt_id, "worker.service", utc_now()))
    assert main(human_status_args) == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "Backup activity ACTIVE" in output
    assert request.attempt_id in output


def test_human_receipt_does_not_imply_worker_finished(
    human_status_args, store, monkeypatch, capsys
):
    request = make_request(make_instance(instance_id="app"))
    store.write_request(request)
    store.write_instance(InstanceState(instance_id="app", active_attempt_id=request.attempt_id))
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
    monkeypatch.setattr(
        "vaultkeep.trigger.cli.RealSystemdController.unit_state",
        lambda _self, _unit: UnitState("loaded", "activating", "start", job_pending=True),
    )
    before = {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    assert main(human_status_args) == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "Backup activity ACTIVE" in output
    assert "Worker RUNNING (activating/start)" in output
    assert before == {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("evidence", ["missing", "blocked", "corrupt", "corrupt_request"])
def test_human_uncertain_activity_is_unknown(human_status_args, store, capsys, evidence):
    if evidence != "missing":
        store.write_instance(InstanceState(instance_id="app"))
    if evidence == "blocked":
        store.write_blocked("app", "requires review")
    elif evidence == "corrupt":
        store.instance_path("app").write_text("{corrupt")
    elif evidence == "corrupt_request":
        request = make_request(make_instance(instance_id="app"))
        store.write_request(request)
        store.request_path(request.attempt_id).write_text("{corrupt")
    assert main(human_status_args) == (2 if evidence.startswith("corrupt") else 0)
    assert "Backup activity" in (output := capsys.readouterr().out)
    assert "UNKNOWN" in output
    assert "Worker service" not in output


def test_human_corrupt_block_prevents_claiming_worker_activity(
    human_status_args, store, monkeypatch, capsys
):
    request = make_request(make_instance(instance_id="app"))
    store.write_instance(InstanceState(instance_id="app", active_attempt_id=request.attempt_id))
    store.write_blocked("app", "requires review")
    store.blocked_path("app").write_text("{corrupt")

    def unexpected(*_args):
        raise AssertionError("corrupt state must not imply worker activity")

    monkeypatch.setattr("vaultkeep.trigger.cli.RealSystemdController.unit_state", unexpected)
    assert main(human_status_args) == 2
    output = " ".join(capsys.readouterr().out.split())
    assert "Backup activity UNKNOWN" in output
    assert "Dispatch blocked" in output


@pytest.fixture
def last_attempt(store):
    request = make_request(make_instance(instance_id="app"))
    state = InstanceState(
        instance_id="app",
        attempt_summaries=[
            {
                "attempt_id": request.attempt_id,
                "request_id": request.request_id,
                "outcome": "backup_failed",
                "exit_code": 5,
            }
        ],
    )
    store.write_request(request)
    store.write_instance(state)
    return state


def test_human_failed_backup_shows_saved_worker_error(
    human_status_args, last_attempt, store, capsys
):
    from vaultkeep.trigger.executor import exec_worker

    attempt_id = last_attempt.attempt_summaries[-1]["attempt_id"]
    result = exec_worker(
        attempt_id,
        store=store,
        provider=FakeProvider([]),
        runner=FakeRunner(
            [
                child_result(exit_code=0, stdout_tail=installed_version()),
                child_result(
                    exit_code=5, diagnostics=b"FAILED: destination mount is unavailable\n"
                ),
            ]
        ),
    )
    assert result.outcome is AttemptOutcome.BACKUP_FAILED
    before = {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    assert main(human_status_args) == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "Backup activity IDLE" in output
    assert "Last attempt outcome backup_failed" in output
    assert f"Last attempt {attempt_id}" in output
    assert "Backup exit code 5" in output
    assert "Diagnostic tail FAILED: destination mount is unavailable" in output
    assert str(store.log_path(attempt_id)) in output
    assert before == {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()}


@pytest.mark.parametrize(
    ("outcome", "reason", "exit_code", "signal_number"),
    [
        ("backup_failed", None, None, 15),
        ("integration_error", "failed to execute VaultKeep: permission denied", None, None),
        ("blocked", "final recheck returned running", None, None),
        ("outcome_unknown", "worker is absent and no completion evidence was recorded", None, None),
        ("deferred", "start submission rejected", 10, None),
    ],
)
def test_human_attempt_reason_without_diagnostics(
    human_status_args, last_attempt, store, capsys, outcome, reason, exit_code, signal_number
):
    summary = last_attempt.attempt_summaries[-1]
    summary.update(outcome=outcome, reason=reason, exit_code=exit_code, signal=signal_number)
    store.write_instance(last_attempt)
    assert main(human_status_args) == 0
    output = " ".join(capsys.readouterr().out.split())
    assert f"Last attempt outcome {outcome}" in output
    if reason:
        assert f"Attempt reason {reason}" in output
    if exit_code is not None:
        assert f"Backup exit code {exit_code}" in output
    if signal_number is not None:
        assert f"Backup signal {signal_number}" in output
    assert "Diagnostics unavailable (not recorded or no longer retained)" in output


@pytest.mark.parametrize("outcome", ["created", "unchanged", "result_unknown"])
def test_human_success_does_not_show_old_failure_output(
    human_status_args, last_attempt, store, capsys, outcome
):
    summary = last_attempt.attempt_summaries[-1]
    store.write_diagnostics(summary["attempt_id"], b"FAILED: old attempt\n")
    last_attempt.attempt_summaries.append(
        {
            **summary,
            "outcome": outcome,
            "attempt_id": "new-attempt",
            "exit_code": 0,
        }
    )
    store.write_instance(last_attempt)
    assert main(human_status_args) == 0
    output = " ".join(capsys.readouterr().out.split())
    assert f"Last attempt outcome {outcome}" in output
    assert "old attempt" not in output
    assert "Diagnostics" not in output


@pytest.mark.parametrize("content", [b"", b"\n \n", b"FAILED: invalid filename \xff\n"])
def test_human_diagnostics_handles_empty_or_invalid_text(
    human_status_args, last_attempt, store, capsys, content
):
    store.write_diagnostics(last_attempt.attempt_summaries[-1]["attempt_id"], content)
    assert main(human_status_args) == 0
    output = " ".join(capsys.readouterr().out.split())
    if content.strip():
        assert "FAILED: invalid filename \ufffd" in output
    else:
        assert "Diagnostics no output recorded" in output


def test_human_diagnostics_unreadable_preserves_outcome(
    human_status_args, last_attempt, store, monkeypatch, capsys
):
    log = store.log_path(last_attempt.attempt_summaries[-1]["attempt_id"])
    original_open = Path.open

    def open_log(path, *args, **kwargs):
        if path == log:
            raise PermissionError("cannot read diagnostics")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_log)
    assert main(human_status_args) == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "Last attempt outcome backup_failed" in output
    assert "Diagnostics unavailable (cannot read diagnostics)" in output


def test_human_diagnostic_tail_is_bounded(human_status_args, last_attempt, store, capsys):
    attempt_id = last_attempt.attempt_summaries[-1]["attempt_id"]
    store.write_diagnostics(
        attempt_id,
        b"omitted old output\n"
        + b"x" * 300000
        + b"\n"
        + b"details\n" * 9
        + b"FAILED: final error\n",
    )
    assert main(human_status_args) == 0
    output = capsys.readouterr().out
    assert "omitted old output" not in output
    assert "xxxx" not in output
    assert "FAILED: final error" in output
    assert len(output) < 2000


def test_human_diagnostic_line_is_bounded_and_has_no_control_characters(
    human_status_args, last_attempt, store, capsys
):
    store.write_diagnostics(
        last_attempt.attempt_summaries[-1]["attempt_id"],
        b"\x1b[31mFAILED: " + b"x" * 2000,
    )
    assert main(human_status_args) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    diagnostic_line = next(line for line in output.splitlines() if "Diagnostic tail" in line)
    assert diagnostic_line.endswith("...")
    assert len(diagnostic_line) < 550


def test_human_invalid_summary_attempt_cannot_read_outside_state(
    human_status_args, last_attempt, store, capsys
):
    last_attempt.attempt_summaries[-1]["attempt_id"] = "../../outside"
    store.write_instance(last_attempt)
    assert main(human_status_args) == 2
    assert "invalid last attempt ID" in capsys.readouterr().err
