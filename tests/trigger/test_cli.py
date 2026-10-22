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

from .conftest import make_instance, make_request


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
    assert "No monitoring state recorded" in capsys.readouterr().out
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


@pytest.fixture
def human_status_args(tmp_path, valid_config, store, monkeypatch):
    config = job(tmp_path, valid_config)
    monkeypatch.setattr("vaultkeep.trigger.cli.state_root", lambda _scope: store.root)
    monkeypatch.setattr("vaultkeep.trigger.cli._monitor_runtime_status", lambda _scope: "running")
    monkeypatch.setattr(
        "vaultkeep.trigger.cli.RealSystemdController.unit_state", lambda _self, _unit: None
    )
    monkeypatch.setattr(
        "vaultkeep.trigger.cli._monitor_started_at", lambda _scope: "2026-01-01T00:00:00Z"
    )
    return ["--user", "--config", str(config), "trigger", "status"]


def _verbose(args):
    path = Path(args[2])
    config = json.loads(path.read_text())
    config["logging"]["level"] = "debug"
    path.write_text(json.dumps(config))


def _output(args, capsys):
    assert main(args) == 0
    captured = capsys.readouterr()
    return " ".join(captured.out.split()), captured


def _summary(request, outcome="backup_failed", **kwargs):
    return {
        "attempt_id": request.attempt_id,
        "request_id": request.request_id,
        "outcome": outcome,
        **kwargs,
    }


@pytest.fixture
def last_attempt(store):
    request = make_request(make_instance(instance_id="app"))
    state = InstanceState(
        instance_id="app",
        last_monitor_update=utc_now(),
        attempt_summaries=[_summary(request, exit_code=5)],
    )
    store.write_request(request)
    store.write_instance(state)
    return state


def test_healthy_idle_report(human_status_args, store, capsys):
    _enable_trigger(human_status_args)
    from vaultkeep.trigger.models import Observation, ObservationRecord

    request = make_request(make_instance(instance_id="app"))
    store.write_instance(
        InstanceState(
            instance_id="app",
            armed=True,
            last_monitor_update=utc_now(),
            config_fingerprint=load_config(Path(human_status_args[2])).instances[0].fingerprint(),
            last_observation=ObservationRecord(Observation.RUNNING, utc_now()),
            attempt_summaries=[_summary(request, "created")],
        )
    )
    output, captured = _output(human_status_args, capsys)
    assert "Application RUNNING (observed 0s ago) Backup IDLE" in output
    assert "Trigger armed" not in output
    assert "Monitor: RUNNING (updated 0s ago)" in output
    assert "Previous attempt Result SUCCESS" in output
    assert "Outstanding issue" not in output
    assert "Observation" not in output
    assert request.attempt_id not in output
    assert "Diagnostics" not in output
    assert captured.err == ""
    assert len(captured.out.splitlines()) < 18
    assert output.count("Job:") == 1


@pytest.mark.parametrize(
    ("worker", "expected"),
    [
        (UnitState("loaded", "activating", "start", True), "UNKNOWN"),
        (UnitState("loaded", "activating", "start-pre", True), "UNKNOWN"),
        (UnitState("loaded", "active", "running"), "UNKNOWN"),
        (UnitState("loaded", "inactive", "dead", True), "QUEUED"),
        (UnitState("loaded", "deactivating", "stop", True), "STOPPING"),
        (UnitState("loaded", "inactive", "dead"), "STOPPED"),
        (UnitState("loaded", "failed", "failed"), "FAILED"),
        (UnitState("not-found", "inactive", "dead"), "STOPPED"),
        (UnitState("loaded", "active", "exited"), "UNKNOWN"),
        (None, "UNKNOWN"),
    ],
)
def test_current_worker(human_status_args, store, monkeypatch, capsys, worker, expected):
    request = make_request(make_instance(instance_id="app"))
    store.write_request(request)
    store.write_instance(InstanceState(instance_id="app", active_attempt_id=request.attempt_id))
    probes = []

    def probe(_self, unit):
        probes.append(unit)
        return worker

    monkeypatch.setattr("vaultkeep.trigger.cli.RealSystemdController.unit_state", probe)
    output, _ = _output(human_status_args, capsys)
    assert probes == [
        unit_name_for(request.attempt_id, load_config(Path(human_status_args[2])).scope)
    ]
    assert f"Application UNKNOWN Backup {expected}" in output
    assert "Worker service" not in output
    assert (request.attempt_id in output) == (expected in ("UNKNOWN", "STOPPED", "FAILED"))


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        (UnitState("not-found", "inactive", "dead"), "NOT INSTALLED"),
        (UnitState("loaded", "inactive", "dead"), "STOPPED"),
        (UnitState("loaded", "failed", "failed"), "FAILED"),
        (UnitState("loaded", "activating", "start"), "STARTING"),
        (None, "STOPPED"),
    ],
)
def test_monitor_service_status(human_status_args, monkeypatch, capsys, unit, expected):
    monkeypatch.setattr(
        "vaultkeep.trigger.cli._monitor_runtime_status", lambda _scope: "not running"
    )
    monkeypatch.setattr(
        "vaultkeep.trigger.cli.RealSystemdController.unit_state", lambda _self, _unit: unit
    )
    output, _ = _output(human_status_args, capsys)
    assert f"Monitor: {expected} (updated unknown)" in output
    assert "Trigger: DISABLED" in output
    assert "No monitoring state recorded" in output
    assert "Previous attempt Result NONE" in output


@pytest.mark.parametrize("with_state", [False, True])
def test_unassociated_worker(human_status_args, store, capsys, with_state):
    if with_state:
        store.write_instance(InstanceState(instance_id="app"))
    request = make_request(make_instance(instance_id="app"))
    store.write_request(request)
    store.write_launch(LaunchRecord(request.attempt_id, "worker.service", utc_now()))
    output, _ = _output(human_status_args, capsys)
    assert "Backup UNKNOWN" in output
    assert request.attempt_id in output


def test_current_attempt_and_previous_failure(
    human_status_args, last_attempt, store, monkeypatch, capsys
):
    current = make_request(make_instance(instance_id="app"))
    store.write_request(current)
    from vaultkeep.progress import ProgressReporter

    ProgressReporter(store.progress_path(current.attempt_id), state="running").report(
        "Creating and compressing archive"
    )
    last_attempt.active_attempt_id = current.attempt_id
    store.write_instance(last_attempt)
    monkeypatch.setattr(
        "vaultkeep.trigger.cli.RealSystemdController.unit_state",
        lambda _self, _unit: UnitState("loaded", "activating", "start"),
    )
    output, _ = _output(human_status_args, capsys)
    assert "Application UNKNOWN Backup RUNNING: Creating and compressing archive" in output
    assert "Previous attempt Result FAILED (exit code 5)" in output
    assert current.attempt_id not in output
    assert last_attempt.attempt_summaries[0]["attempt_id"] not in output


def test_receipt_does_not_complete_worker_or_replace_previous_history(
    human_status_args, last_attempt, store, monkeypatch, capsys
):
    request = make_request(make_instance(instance_id="app"))
    store.write_request(request)
    store.write_receipt(
        Receipt(
            request.attempt_id,
            request.request_id,
            "app",
            AttemptOutcome.CREATED,
            utc_now(),
            utc_now(),
            1.0,
        )
    )
    last_attempt.active_attempt_id = request.attempt_id
    last_attempt.attempt_summaries.append(_summary(request, "created"))
    store.write_instance(last_attempt)
    monkeypatch.setattr(
        "vaultkeep.trigger.cli.RealSystemdController.unit_state",
        lambda _self, _unit: UnitState("loaded", "activating", "start"),
    )
    before = {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    output, _ = _output(human_status_args, capsys)
    assert "Backup COMPLETED (worker awaiting reconciliation) Result SUCCESS" in output
    assert "Child completed" not in output
    assert "Previous attempt Result FAILED" in output
    assert before == {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    "outcome",
    [
        "created",
        "unchanged",
        "result_unknown",
        "backup_failed",
        "integration_error",
        "blocked",
        "deferred",
        "outcome_unknown",
    ],
)
def test_completed_results(human_status_args, last_attempt, store, capsys, outcome):
    last_attempt.attempt_summaries[0].update(outcome=outcome, recorded_at="2026-10-22T09:47:00Z")
    store.write_instance(last_attempt)
    output, _ = _output(human_status_args, capsys)
    assert "Previous attempt" in output
    assert "Recorded" not in output
    assert "Completed" not in output  # Summary time cannot establish completion time.
    assert "Diagnostic tail" not in output
    assert "Diagnostics path" not in output
    if outcome == "result_unknown":
        assert "archive creation was not confirmed" in output


def test_finalization_error_has_reason_and_existing_log(
    human_status_args, last_attempt, store, capsys
):
    attempt_id = last_attempt.attempt_summaries[0]["attempt_id"]
    path = "/srv/backups/bisq/with spaces/backup-20261022-094742"
    store.write_diagnostics(
        attempt_id,
        (
            f"FAILED: Cannot atomically finalize backup {path}: "
            "[Errno 13] Permission denied: PosixPath('internal')\n"
        ).encode(),
    )
    output, _ = _output(human_status_args, capsys)
    assert "Reason Permission denied during finalization" in output
    assert path not in output
    assert f"Log {store.log_path(attempt_id)}" in output
    assert "PosixPath" not in output
    assert "Outstanding issue" not in output
    assert "corrupt" not in output


@pytest.mark.parametrize("evidence", ["missing", "blocked", "corrupt", "corrupt_request"])
def test_uncertain_state(human_status_args, store, capsys, evidence):
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
    output = capsys.readouterr().out
    assert "UNKNOWN" in output
    assert "Vaultkeep — Trigger status" in output


@pytest.mark.parametrize("resolved", [False, True])
def test_status_respects_existing_aggregate_review_state(
    human_status_args, last_attempt, store, capsys, resolved
):
    attempt_id = last_attempt.attempt_summaries[0]["attempt_id"]
    last_attempt.review_required = not resolved
    last_attempt.review_reason = "monitoring gap overlapped possible execution"
    last_attempt.attempt_summaries[0]["review_required"] = True
    last_attempt.attempt_summaries.append(
        _summary(
            make_request(make_instance(instance_id="app")),
            "unchanged" if not resolved else "created",
        )
    )
    store.write_instance(last_attempt)
    before = store.instance_path("app").read_bytes()
    output, _ = _output(human_status_args, capsys)
    assert "Outstanding issue" not in output
    assert attempt_id not in output
    assert store.instance_path("app").read_bytes() == before
    _verbose(human_status_args)
    output, _ = _output(human_status_args, capsys)
    assert ("Outstanding issue" in output) == (not resolved)
    if not resolved:
        assert attempt_id in output
        assert "Monitoring was interrupted during possible backup execution" in output
        assert "Exact execution interval unavailable" in output
        assert "missed" not in output
        assert "archive corruption is not established" in output


def test_legacy_unidentified_review_is_not_hidden(human_status_args, store, capsys):
    store.write_instance(InstanceState(instance_id="app", review_required=True))
    _verbose(human_status_args)
    output, _ = _output(human_status_args, capsys)
    assert "Affected attempt Not identified in retained state" in output
    assert "Action Review retained attempt records" in output


@pytest.mark.parametrize(
    "observation", ["stopped", "running", "ambiguous", "inspection_unavailable"]
)
def test_observation_problems(human_status_args, store, capsys, observation):
    _enable_trigger(human_status_args)
    from vaultkeep.trigger.models import Observation, ObservationRecord

    store.write_instance(
        InstanceState(
            instance_id="app",
            config_fingerprint=load_config(Path(human_status_args[2])).instances[0].fingerprint(),
            last_observation=ObservationRecord(Observation(observation), utc_now()),
            last_monitor_update=utc_now(),
        )
    )
    output, _ = _output(human_status_args, capsys)
    if observation == "running":
        assert "Application RUNNING (observed" in output
    elif observation == "stopped":
        assert "Application NOT YET DETECTED" in output
    else:
        assert "Application UNKNOWN" in output
        assert (
            "identity ambiguous" if observation == "ambiguous" else "inspection unavailable"
        ) in output


@pytest.mark.parametrize("verbose", [False, True])
def test_diagnostics_verbosity(human_status_args, last_attempt, store, capsys, verbose):
    attempt_id = last_attempt.attempt_summaries[0]["attempt_id"]
    store.write_diagnostics(attempt_id, b"FAILED: destination mount is unavailable\n")
    if verbose:
        _verbose(human_status_args)
    output, captured = _output(human_status_args, capsys)
    assert "Previous attempt Result FAILED" in output
    assert ("Diagnostic tail FAILED:" in output) == verbose
    assert str(store.log_path(attempt_id)) in output
    assert ("Trigger state root:" in captured.err) == verbose
    assert "INFO:" not in captured.err


@pytest.mark.parametrize("content", [b"", b"\n \n", b"FAILED: invalid filename \xff\n"])
def test_debug_diagnostics_invalid_or_empty(
    human_status_args, last_attempt, store, capsys, content
):
    _verbose(human_status_args)
    store.write_diagnostics(last_attempt.attempt_summaries[0]["attempt_id"], content)
    output, _ = _output(human_status_args, capsys)
    assert (
        "invalid filename \ufffd" in output
        if content.strip()
        else "Diagnostics no output recorded" in output
    )


def test_debug_diagnostics_bounded(human_status_args, last_attempt, store, capsys):
    _verbose(human_status_args)
    store.write_diagnostics(
        last_attempt.attempt_summaries[0]["attempt_id"],
        b"old output\n" + b"x" * 300000 + b"\n" + b"details\n" * 9 + b"FAILED: final error\n",
    )
    output, _ = _output(human_status_args, capsys)
    assert "old output" not in output and "xxxx" not in output
    assert "FAILED: final error" in output


def test_missing_diagnostics_preserves_failure(
    human_status_args, last_attempt, store, monkeypatch, capsys
):
    path = store.log_path(last_attempt.attempt_summaries[0]["attempt_id"])
    original = Path.open

    def open_log(self, *args, **kwargs):
        if self == path:
            raise PermissionError("cannot read diagnostics")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_log)
    output, _ = _output(human_status_args, capsys)
    assert "FAILED (exit code 5)" in output
    assert "Reason Not recorded or unavailable" in output


def test_invalid_attempt_cannot_read_outside_state(human_status_args, last_attempt, store, capsys):
    last_attempt.attempt_summaries[0]["attempt_id"] = "../../outside"
    store.write_instance(last_attempt)
    assert main(human_status_args) == 2
    assert "invalid last attempt ID" in capsys.readouterr().err


def _enable_trigger(args):
    from .test_config import trigger_settings

    path = Path(args[2])
    config = json.loads(path.read_text())
    config["trigger"] = trigger_settings()
    path.write_text(json.dumps(config))


@pytest.mark.parametrize("update", [None, "2020-01-01T00:00:00Z", "2999-01-01T00:00:00Z"])
def test_running_lock_with_unreliable_freshness(human_status_args, store, capsys, update):
    _enable_trigger(human_status_args)
    store.write_instance(InstanceState(instance_id="app", last_monitor_update=update))
    output, _ = _output(human_status_args, capsys)
    assert "Monitor: UNKNOWN" in output
    assert "updates stale or unavailable; check monitor journal" in output


def test_enabled_stopped_monitor_has_action(human_status_args, monkeypatch, capsys):
    _enable_trigger(human_status_args)
    monkeypatch.setattr(
        "vaultkeep.trigger.cli._monitor_runtime_status", lambda _scope: "not running"
    )
    output, _ = _output(human_status_args, capsys)
    assert "Monitor: STOPPED (updated unknown; check monitor service and journal)" in output
    assert "Application UNKNOWN" in output


def test_clean_receipt_awaiting_worker_does_not_resolve_earlier_risk(
    human_status_args, last_attempt, store, capsys
):
    previous_id = last_attempt.attempt_summaries[0]["attempt_id"]
    current = make_request(make_instance(instance_id="app"))
    last_attempt.review_required = True
    last_attempt.attempt_summaries[0]["review_required"] = True
    last_attempt.active_attempt_id = current.attempt_id
    last_attempt.attempt_summaries.append(_summary(current, "created"))
    store.write_instance(last_attempt)
    output, _ = _output(human_status_args, capsys)
    assert "Outstanding issue" not in output
    _verbose(human_status_args)
    output, _ = _output(human_status_args, capsys)
    assert previous_id in output.split("Outstanding issue")[1]


def test_unknown_result_is_explained_once(human_status_args, last_attempt, store, capsys):
    last_attempt.review_required = True
    last_attempt.attempt_summaries[0].update(outcome="outcome_unknown", review_required=True)
    store.write_instance(last_attempt)
    output, _ = _output(human_status_args, capsys)
    assert "Backup completion could not be verified; do not replay" in output
    assert "Outstanding issue" not in output


def test_completed_receipt_supplies_execution_interval(
    human_status_args, last_attempt, store, capsys
):
    summary = last_attempt.attempt_summaries[0]
    store.write_receipt(
        Receipt(
            summary["attempt_id"],
            summary["request_id"],
            "app",
            AttemptOutcome.CREATED,
            "2026-10-22T09:30:00Z",
            "2026-10-22T09:35:00Z",
            300,
        )
    )
    summary.update(outcome="created", review_required=True)
    last_attempt.review_required = True
    store.write_instance(last_attempt)
    output, _ = _output(human_status_args, capsys)
    assert "Result SUCCESS Completed 2026-10-22 09:35 UTC" in output
    assert "Outstanding issue" not in output
    _verbose(human_status_args)
    output, _ = _output(human_status_args, capsys)
    assert "Execution period 2026-10-22 09:30:00 UTC — 2026-10-22 09:35:00 UTC" in output


def test_corrupt_block_does_not_probe_worker(human_status_args, store, monkeypatch, capsys):
    request = make_request(make_instance(instance_id="app"))
    store.write_instance(InstanceState(instance_id="app", active_attempt_id=request.attempt_id))
    store.write_blocked("app", "requires review")
    store.blocked_path("app").write_text("{corrupt")

    def unexpected(*_args):
        raise AssertionError("corrupt block must not imply worker activity")

    monkeypatch.setattr("vaultkeep.trigger.cli.RealSystemdController.unit_state", unexpected)
    assert main(human_status_args) == 2
    output = " ".join(capsys.readouterr().out.split())
    assert "Backup BLOCKED" in output
    assert "Dispatch BLOCKED (recovery state; preserve records)" in output


@pytest.mark.parametrize(
    ("terminal", "flag", "environment", "colored"),
    [
        (True, False, False, True),
        (True, True, False, False),
        (True, False, True, False),
        (False, False, False, False),
    ],
)
def test_status_color_and_redirection(
    human_status_args, last_attempt, monkeypatch, terminal, flag, environment, colored
):
    import io
    import sys

    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: terminal)
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("NO_COLOR", "1" if environment else "")
    args = ["--no-color", *human_status_args] if flag else human_status_args
    assert main(args) == 0
    rendered = stream.getvalue()
    assert ("\x1b[" in rendered) == colored
    if colored:
        assert "\x1b[36mRUNNING\x1b[0m (updated" in rendered
        assert "\x1b[1;31mFAILED\x1b[0m (exit code 5)" in rendered
    assert "INFO:" not in rendered


def test_debug_diagnostic_line_sanitized_and_bounded(
    human_status_args, last_attempt, store, capsys
):
    _verbose(human_status_args)
    store.write_diagnostics(
        last_attempt.attempt_summaries[0]["attempt_id"], b"\x1b[31mFAILED: " + b"x" * 2000
    )
    _, captured = _output(human_status_args, capsys)
    assert "\x1b" not in captured.out
    line = next(line for line in captured.out.splitlines() if "Diagnostic tail" in line)
    assert line.endswith("...") and len(line) < 550


@pytest.mark.parametrize("mismatch", ["instance", "request", "outcome"])
def test_contradictory_receipt_is_not_presented_as_history(
    human_status_args, last_attempt, store, capsys, mismatch
):
    summary = last_attempt.attempt_summaries[0]
    store.write_receipt(
        Receipt(
            summary["attempt_id"],
            "different-request" if mismatch == "request" else summary["request_id"],
            "different-instance" if mismatch == "instance" else "app",
            AttemptOutcome.CREATED if mismatch == "outcome" else AttemptOutcome.BACKUP_FAILED,
            utc_now(),
            utc_now(),
            1.0,
        )
    )
    assert main(human_status_args) == 2
    captured = capsys.readouterr()
    assert "contradictory" in captured.err
    assert "Completed" not in captured.out


@pytest.mark.parametrize("pending", [False, True])
def test_submission_rejection_relevance(human_status_args, store, capsys, pending):
    from vaultkeep.trigger.models import RejectionState

    _enable_trigger(human_status_args)
    request = make_request(make_instance(instance_id="app"))
    store.write_instance(
        InstanceState(
            instance_id="app",
            last_monitor_update=utc_now(),
            pending_request=replace(request, attempt_id="") if pending else None,
            rejection=RejectionState(request_id=request.request_id, consecutive=5),
        )
    )
    output, _ = _output(human_status_args, capsys)
    assert ("Submission rejected; check worker installation and permissions" in output) == pending
    assert ("Backup PENDING" in output) == pending


@pytest.fixture
def fresh_status(human_status_args, monkeypatch):
    _enable_trigger(human_status_args)
    for module in ("cli", "status", "activity"):
        monkeypatch.setattr(f"vaultkeep.trigger.{module}.utc_now", lambda: "2026-10-22T10:00:00Z")
    monkeypatch.setattr(
        "vaultkeep.trigger.cli._monitor_started_at", lambda _scope: "2026-10-22T09:00:00Z"
    )
    instance = load_config(Path(human_status_args[2])).instances[0]
    state = InstanceState(
        instance_id=instance.id,
        config_fingerprint=instance.fingerprint(),
        last_monitor_update="2026-10-22T09:59:59Z",
    )
    return human_status_args, instance, state


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("cold", "NOT YET DETECTED"),
        ("running", "RUNNING (observed 1s ago)"),
        ("confirming", "CONFIRMING STOP (shutdown not yet confirmed)"),
        ("confirmed", "HAS STOPPED (15s ago)"),
    ],
)
def test_application_lifecycle_report(fresh_status, store, capsys, stage, expected):
    from vaultkeep.trigger.detection import DetectionResult
    from vaultkeep.trigger.lifecycle import ExecutionContext, progress
    from vaultkeep.trigger.models import Observation

    args, instance, state = fresh_status
    context = ExecutionContext(vaultkeep="/usr/local/bin/vaultkeep", environment={})

    def observe(observation, monotonic, timestamp):
        progress(
            state,
            instance,
            DetectionResult(observation),
            context,
            monotonic_now=monotonic,
            utc_now_value=timestamp,
        )

    if stage == "cold":
        observe(Observation.STOPPED, 0, "2026-10-22T09:59:59Z")
    elif stage == "running":
        observe(Observation.RUNNING, 0, "2026-10-22T09:59:59Z")
    else:
        observe(Observation.RUNNING, 0, "2026-10-22T09:59:40Z")
        if stage == "confirmed":
            observe(Observation.STOPPED, 1, "2026-10-22T09:59:41Z")
            observe(Observation.STOPPED, 7, "2026-10-22T09:59:45Z")
        observe(Observation.STOPPED, 21, "2026-10-22T09:59:59Z")
    store.write_instance(state)
    output, _ = _output(args, capsys)
    assert f"Application {expected}" in output
    assert "Monitor: RUNNING (updated 1s ago)" in output
    assert "Previous attempt Result NONE" in output
    assert "Trigger armed" not in output


@pytest.mark.parametrize("active", [False, True])
def test_confirmed_shutdown_from_retained_request(fresh_status, store, capsys, active):
    from vaultkeep.trigger.models import Observation, ObservationRecord

    args, instance, state = fresh_status
    request = replace(make_request(instance), confirmed_at="2026-10-22T09:59:45Z")
    state.latest_request_id = request.request_id
    state.last_observation = ObservationRecord(Observation.STOPPED, "2026-10-22T09:59:59Z")
    if active:
        state.active_attempt_id = request.attempt_id
    else:
        state.attempt_summaries.append(_summary(request, "created"))
    store.write_request(request)
    store.write_instance(state)
    output, _ = _output(args, capsys)
    assert "Application HAS STOPPED (15s ago)" in output


@pytest.mark.parametrize(
    ("candidate_since", "expected"),
    [
        ("2026-10-22T09:59:55Z", "CONFIRMING STOP (shutdown not yet confirmed)"),
        ("2026-10-22T08:59:59Z", "UNKNOWN (shutdown not yet confirmed)"),
        ("2026-10-22T10:00:00Z", "UNKNOWN (shutdown not yet confirmed)"),
        (None, "UNKNOWN"),
    ],
)
def test_stop_confirmation_requires_current_session_candidate(
    fresh_status, store, capsys, candidate_since, expected
):
    from vaultkeep.trigger.models import Observation, ObservationRecord

    args, instance, state = fresh_status
    request = replace(make_request(instance), attempt_id="")
    state.pending_request = request
    state.pending_reconfirmation_required = True
    state.candidate_since = candidate_since
    state.last_observation = ObservationRecord(Observation.STOPPED, "2026-10-22T09:59:59Z")
    store.write_instance(state)
    output, _ = _output(args, capsys)
    assert f"Application {expected}" in output
    assert "Backup PENDING" in output


@pytest.mark.parametrize(
    "problem",
    [
        "stale",
        "missing",
        "future",
        "before_session",
        "session_unavailable",
        "stopped_monitor",
        "configuration_changed",
        "disabled",
        "unconfirmed",
        "old_shutdown",
        "invalidated_request",
    ],
)
def test_unreliable_application_state(fresh_status, store, monkeypatch, capsys, problem):
    from vaultkeep.trigger.models import Observation, ObservationRecord

    args, instance, state = fresh_status
    timestamp = "2026-10-22T09:59:59Z"
    if problem == "stale":
        timestamp = "2026-10-22T09:59:00Z"
    elif problem == "future":
        timestamp = "2026-10-22T10:00:01Z"
    state.last_observation = ObservationRecord(Observation.RUNNING, timestamp)
    if problem == "missing":
        state.last_observation = None
    elif problem == "before_session":
        monkeypatch.setattr(
            "vaultkeep.trigger.cli._monitor_started_at", lambda _scope: "2026-10-22T10:00:00Z"
        )
    elif problem == "session_unavailable":
        monkeypatch.setattr("vaultkeep.trigger.cli._monitor_started_at", lambda _scope: None)
    elif problem == "stopped_monitor":
        monkeypatch.setattr(
            "vaultkeep.trigger.cli._monitor_runtime_status", lambda _scope: "stopped"
        )
    elif problem == "configuration_changed":
        state.config_fingerprint = "old fingerprint"
    elif problem == "disabled":
        path = Path(args[2])
        config = json.loads(path.read_text())
        config["trigger"]["enabled"] = False
        path.write_text(json.dumps(config))
    elif problem in ("unconfirmed", "old_shutdown", "invalidated_request"):
        state.last_observation = ObservationRecord(Observation.STOPPED, timestamp)
        if problem == "unconfirmed":
            state.armed = True  # May have been inherited from a previous monitor session.
        elif problem == "invalidated_request":
            state.latest_request_id = ""  # A later RUNNING observation invalidated the shutdown.
        else:
            request = replace(make_request(instance), confirmed_at="2026-10-22T08:00:00Z")
            state.latest_request_id = request.request_id
            state.pending_request = replace(request, attempt_id="")
    store.write_instance(state)
    output, _ = _output(args, capsys)
    assert "Application UNKNOWN" in output
    assert "Application HAS STOPPED" not in output
    assert "Application NOT YET DETECTED" not in output


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (10, "10s"),
        (59.9, "59s"),
        (60, "1m 0s"),
        (155, "2m 35s"),
        (3600, "1h 0m"),
        (4320, "1h 12m"),
    ],
)
def test_compact_duration(seconds, expected):
    from vaultkeep.trigger.activity import duration

    assert duration(seconds) == expected


@pytest.mark.parametrize("worker", ["activating", "active"])
@pytest.mark.parametrize(
    "timestamp", ["2026-10-22T09:57:25Z", None, "invalid", "2999-01-01T00:00:00Z"]
)
def test_worker_elapsed_since_dispatch(fresh_status, store, monkeypatch, capsys, worker, timestamp):
    args, instance, state = fresh_status
    request = make_request(instance)
    state.active_attempt_id = request.attempt_id
    store.write_request(request)
    store.write_instance(state)
    if timestamp:
        store.write_launch(LaunchRecord(request.attempt_id, "worker.service", timestamp))
    monkeypatch.setattr(
        "vaultkeep.trigger.cli.RealSystemdController.unit_state",
        lambda _self, _unit: UnitState(
            "loaded", worker, "start" if worker == "activating" else "running"
        ),
    )
    output, captured = _output(args, capsys)
    expected = "UNKNOWN (backup progress unavailable)"
    backup_line = next(
        line for line in captured.out.splitlines() if line.strip().startswith("Backup")
    )
    if timestamp == "2026-10-22T09:57:25Z":
        assert " ".join(backup_line.split()) == f"Backup {expected} (2m 35s since dispatch)"
    else:
        assert " ".join(backup_line.split()) == f"Backup {expected}"
    assert "Previous attempt Result NONE" in output


@pytest.fixture
def live_backup(fresh_status, store):
    from vaultkeep.progress import ProgressReporter

    args, instance, state = fresh_status
    request = make_request(instance)
    state.active_attempt_id = request.attempt_id
    store.write_request(request)
    store.write_instance(state)
    path = store.progress_path(request.attempt_id)
    ProgressReporter(path, state="running").report("Creating and compressing archive")
    data = json.loads(path.read_text())
    data.update(
        started_at="2026-10-22T09:58:29Z",
        phase_started_at="2026-10-22T09:59:15Z",
        updated_at="2026-10-22T09:59:59Z",
    )
    path.write_text(json.dumps(data))
    return args, request, path


@pytest.mark.parametrize("monitor", ["running", "not running", "unknown"])
def test_live_backup_reports_phase_and_both_durations_without_systemd(
    live_backup, monkeypatch, capsys, monitor
):
    args, _request, _path = live_backup
    monkeypatch.setattr("vaultkeep.trigger.cli._monitor_runtime_status", lambda _scope: monitor)
    if monitor == "not running":
        # A monitor-service probe is still allowed when its lock is free.
        monkeypatch.setattr(
            "vaultkeep.trigger.status.RealSystemdController.unit_state",
            lambda _self, unit: (
                None if "vaultkeep-trigger-exec@" not in unit else pytest.fail("worker probe")
            ),
        )
    else:
        monkeypatch.setattr(
            "vaultkeep.trigger.status.RealSystemdController.unit_state",
            lambda *_args: pytest.fail("worker progress must not wait for systemd"),
        )
    output, _ = _output(args, capsys)
    assert "Backup RUNNING: Creating and compressing archive (45s / 1m 31s elapsed)" in output
    assert "since dispatch" not in output
    assert "Previous attempt Result NONE" in output


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"state": "preparing"}, "PREPARING: Creating and compressing archive"),
        ({"state": "finished"}, "FINISHING (awaiting backup result)"),
        ({"updated_at": "2026-10-22T09:59:29Z"}, "UNKNOWN (backup progress stale or invalid"),
        ({"updated_at": "2026-10-22T10:00:01Z"}, "UNKNOWN (backup progress stale or invalid"),
        ({"started_at": "invalid"}, "UNKNOWN (backup progress stale or invalid"),
        ({"phase_started_at": "2026-10-22T09:58:28Z"}, "UNKNOWN (backup progress stale or invalid"),
        ({"phase_started_at": "2026-10-22T10:00:00Z"}, "UNKNOWN (backup progress stale or invalid"),
        ({"create_time": 1.0}, "UNKNOWN (backup process unavailable; awaiting result)"),
        ({"attempt_id": "another-attempt"}, "UNKNOWN"),
    ],
)
def test_live_backup_requires_valid_fresh_process_evidence(live_backup, capsys, change, expected):
    args, _request, path = live_backup
    data = json.loads(path.read_text())
    data.update(change)
    path.write_text(json.dumps(data))
    output, _ = _output(args, capsys)
    assert f"Backup {expected}" in output
    if expected.startswith("UNKNOWN"):
        assert "Backup RUNNING" not in output


def test_receipt_takes_precedence_over_live_progress(live_backup, store, capsys):
    args, request, _path = live_backup
    store.write_receipt(
        Receipt(
            request.attempt_id,
            request.request_id,
            request.instance_id,
            AttemptOutcome.BACKUP_FAILED,
            "2026-10-22T09:58:29Z",
            "2026-10-22T09:59:59Z",
            90,
            exit_code=5,
        )
    )
    output, _ = _output(args, capsys)
    assert "Backup COMPLETED (worker awaiting reconciliation) Result FAILED" in output
    assert "Creating and compressing archive" not in output


@pytest.mark.parametrize("outcome", ["created", "unchanged", "backup_failed"])
def test_previous_receipt_and_log(fresh_status, store, capsys, outcome):
    args, instance, state = fresh_status
    request = make_request(instance)
    store.write_request(request)
    state.attempt_summaries.append(
        _summary(request, outcome, exit_code=5 if outcome == "backup_failed" else 0)
    )
    store.write_instance(state)
    store.write_receipt(
        Receipt(
            request.attempt_id,
            request.request_id,
            instance.id,
            AttemptOutcome(outcome),
            "2026-10-22T09:45:00Z",
            "2026-10-22T09:47:00Z",
            120,
            diagnostics_ref="/untrusted/log.txt",
        )
    )
    store.write_diagnostics(request.attempt_id, b"FAILED: destination mount is unavailable\n")
    output, _ = _output(args, capsys)
    result = {
        "created": "SUCCESS",
        "unchanged": "UNCHANGED",
        "backup_failed": "FAILED (exit code 5)",
    }[outcome]
    assert f"Previous attempt Result {result} Completed 2026-10-22 09:47 UTC" in output
    assert (f"Log {store.log_path(request.attempt_id)}" in output) == (outcome == "backup_failed")
    assert "/untrusted/log.txt" not in output
    assert "Diagnostic tail" not in output


@pytest.mark.parametrize("active", [False, True])
def test_current_safety_issue_is_visible(fresh_status, store, monkeypatch, capsys, active):
    args, instance, state = fresh_status
    state.review_required = True
    state.review_reason = "historical problem"
    if active:
        request = make_request(instance)
        state.active_attempt_id = request.attempt_id
        state.active_review_required = True
        state.active_review_reason = "process observation was delayed during execution"
        store.write_request(request)
        store.write_diagnostics(request.attempt_id, b"retained output")
        monkeypatch.setattr(
            "vaultkeep.trigger.cli.RealSystemdController.unit_state",
            lambda _self, _unit: UnitState("loaded", "active", "running"),
        )
    store.write_instance(state)
    before = store.instance_path(instance.id).read_bytes()
    output, _ = _output(args, capsys)
    assert (
        "Safety Application observation was delayed during backup execution" in output
    ) == active
    assert "historical problem" not in output
    assert "Outstanding issue" not in output
    if active:
        assert f"Log {store.log_path(request.attempt_id)}" in output
    assert store.instance_path(instance.id).read_bytes() == before


def test_current_recovery_block_is_visible(fresh_status, store, capsys):
    args, instance, state = fresh_status
    store.write_instance(state)
    store.write_blocked(instance.id, "attempt ownership is contradictory")
    output, _ = _output(args, capsys)
    assert "Backup BLOCKED" in output
    assert "Reason attempt ownership is contradictory" in output
    assert "Follow recovery instructions" in output


def test_missing_log_does_not_invent_path(human_status_args, last_attempt, store, capsys):
    output, _ = _output(human_status_args, capsys)
    assert "FAILED (exit code 5)" in output
    assert str(store.log_path(last_attempt.attempt_summaries[0]["attempt_id"])) not in output


def test_verbose_retains_armed_worker_and_observation(fresh_status, store, monkeypatch, capsys):
    from vaultkeep.trigger.models import Observation, ObservationRecord

    args, instance, state = fresh_status
    request = make_request(instance)
    state.active_attempt_id = request.attempt_id
    state.armed = True
    state.last_observation = ObservationRecord(Observation.RUNNING, "2026-10-22T09:59:59Z")
    store.write_request(request)
    store.write_instance(state)
    _verbose(args)
    output, captured = _output(args, capsys)
    assert "Stored armed yes" in output
    assert f"Active attempt {request.attempt_id}" in output
    assert "Worker service vaultkeep-trigger-exec@" in output
    assert "Monitor service unit:" in captured.err
    assert "Observation running" in output


def test_monitor_session_uses_existing_lock_timestamp(tmp_path, monkeypatch):
    import os

    from vaultkeep.trigger.cli import _monitor_started_at, _monitor_status
    from vaultkeep.trigger.models import parse_utc
    from vaultkeep.trigger.monitor import MonitorLock

    path = tmp_path / "monitor.lock"
    monkeypatch.setattr("vaultkeep.trigger.cli.monitor_lock_path", lambda _scope: path)
    assert _monitor_started_at(None) is None
    with MonitorLock(path):
        assert _monitor_status(None) == "running"
        before = path.read_bytes(), path.stat().st_mtime_ns
        started = parse_utc(_monitor_started_at(None))
        assert started.timestamp() == pytest.approx(path.stat().st_mtime, rel=0, abs=0.000001)
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    # A previous lock owner or an old file timestamp cannot establish the new session.
    os.utime(path, (1, 1))
    with MonitorLock(path):
        assert parse_utc(_monitor_started_at(None)).timestamp() > 1


def test_unreadable_session_timestamp_is_unknown(tmp_path, monkeypatch):
    from vaultkeep.trigger.cli import _monitor_started_at

    monkeypatch.setattr("vaultkeep.trigger.cli.monitor_lock_path", lambda _scope: tmp_path / "lock")

    def denied(*_args, **_kwargs):
        raise PermissionError("cannot read lock metadata")

    monkeypatch.setattr(Path, "stat", denied)
    assert _monitor_started_at(None) is None


def test_application_history_cannot_read_outside_state(fresh_status, store, monkeypatch, capsys):
    from vaultkeep.trigger.models import Observation, ObservationRecord
    from vaultkeep.trigger.state import StateStore

    args, _instance, state = fresh_status
    state.latest_request_id = "request"
    state.last_observation = ObservationRecord(Observation.STOPPED, "2026-10-22T09:59:59Z")
    state.attempt_summaries = [
        {"attempt_id": "../../outside", "request_id": "request", "outcome": "created"}
    ]
    store.write_instance(state)

    def unexpected(*_args):
        raise AssertionError("invalid history must be rejected before reading a request")

    monkeypatch.setattr(StateStore, "read_request", unexpected)
    assert main(args) == 2
    assert "invalid last attempt ID" in capsys.readouterr().err


@pytest.mark.parametrize("level", ["error", "warning", "info", "debug"])
def test_status_report_remains_visible_at_all_logging_levels(fresh_status, store, capsys, level):
    args, _instance, state = fresh_status
    store.write_instance(state)
    path = Path(args[2])
    config = json.loads(path.read_text())
    config["logging"]["level"] = level
    path.write_text(json.dumps(config))
    output, captured = _output(args, capsys)
    assert "Current activity Application UNKNOWN Backup IDLE" in output
    assert "Previous attempt Result NONE" in output
    assert "INFO:" not in captured.out and "ERROR:" not in captured.out


@pytest.mark.parametrize("source", ["summary", "log"])
def test_normal_failure_reason_is_bounded(human_status_args, last_attempt, store, capsys, source):
    summary = last_attempt.attempt_summaries[0]
    reason = "private details " * 100
    if source == "summary":
        summary["reason"] = reason
        store.write_instance(last_attempt)
    else:
        store.write_diagnostics(summary["attempt_id"], f"FAILED: {reason}\n".encode())
    output, _ = _output(human_status_args, capsys)
    assert "Reason See diagnostic details" in output
    assert "private details" not in output
    assert "Diagnostic tail" not in output


def test_invalid_completion_time_is_omitted(human_status_args, last_attempt, store, capsys):
    summary = last_attempt.attempt_summaries[0]
    store.write_receipt(
        Receipt(
            summary["attempt_id"],
            summary["request_id"],
            "app",
            AttemptOutcome.BACKUP_FAILED,
            utc_now(),
            "invalid",
            1.0,
        )
    )
    output, _ = _output(human_status_args, capsys)
    assert "Previous attempt Result FAILED" in output
    assert "Completed" not in output


def test_launch_ownership_cannot_supply_elapsed_time(fresh_status, store, monkeypatch, capsys):
    args, instance, state = fresh_status
    request = make_request(instance)
    state.active_attempt_id = request.attempt_id
    store.write_request(request)
    store.write_instance(state)
    store.write_launch(LaunchRecord(request.attempt_id, "worker.service", "2026-10-22T09:57:25Z"))
    path = store.launch_path(request.attempt_id)
    launch = json.loads(path.read_text())
    launch["attempt_id"] = "different-attempt"
    path.write_text(json.dumps(launch))
    monkeypatch.setattr(
        "vaultkeep.trigger.cli.RealSystemdController.unit_state",
        lambda _self, _unit: UnitState("loaded", "active", "running"),
    )
    assert main(args) == 2
    captured = capsys.readouterr()
    assert "contradictory launch ownership" in captured.err
    assert "since dispatch" not in captured.out
