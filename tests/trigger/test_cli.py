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
    state = InstanceState(instance_id="app", attempt_summaries=[_summary(request, exit_code=5)])
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
            last_observation=ObservationRecord(Observation.STOPPED, utc_now()),
            attempt_summaries=[_summary(request, "created")],
        )
    )
    output, captured = _output(human_status_args, capsys)
    assert "Backup IDLE Trigger armed YES" in output
    assert "Monitor: RUNNING (updated 0s ago)" in output
    assert "Last backup Result SUCCESS" in output
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
        (UnitState("loaded", "activating", "start", True), "STARTING"),
        (UnitState("loaded", "activating", "start-pre", True), "STARTING"),
        (UnitState("loaded", "active", "running"), "RUNNING"),
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
    assert f"Current activity Backup {expected}" in output
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
    assert "Last backup" not in output


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
    last_attempt.active_attempt_id = current.attempt_id
    store.write_instance(last_attempt)
    monkeypatch.setattr(
        "vaultkeep.trigger.cli.RealSystemdController.unit_state",
        lambda _self, _unit: UnitState("loaded", "activating", "start"),
    )
    output, _ = _output(human_status_args, capsys)
    assert "Current activity Backup STARTING" in output
    assert "Last backup Result FAILED (exit code 5)" in output
    assert current.attempt_id not in output
    assert last_attempt.attempt_summaries[0]["attempt_id"] in output


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
    assert "Backup STARTING" in output
    assert "Child completed" in output
    assert "Last backup Result FAILED" in output
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
    assert "Last backup" in output or "Last completed attempt" in output
    assert "Recorded 2026-10-22 09:47:00 UTC" in output
    assert "Completed" not in output  # Summary time cannot establish completion time.
    assert "Diagnostic tail" not in output
    assert "Diagnostics path" not in output
    if outcome == "result_unknown":
        assert "archive creation was not confirmed" in output


def test_finalization_error_identifies_exact_backup(human_status_args, last_attempt, store, capsys):
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
    assert f"Backup {path}" in output
    assert "Action Check destination permissions" in output
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
            last_observation=ObservationRecord(Observation(observation), utc_now()),
            last_monitor_update=utc_now(),
        )
    )
    output, _ = _output(human_status_args, capsys)
    assert ("Monitoring health" in output) == (
        observation in ("ambiguous", "inspection_unavailable")
    )


@pytest.mark.parametrize("verbose", [False, True])
def test_diagnostics_verbosity(human_status_args, last_attempt, store, capsys, verbose):
    attempt_id = last_attempt.attempt_summaries[0]["attempt_id"]
    store.write_diagnostics(attempt_id, b"FAILED: destination mount is unavailable\n")
    if verbose:
        _verbose(human_status_args)
    output, captured = _output(human_status_args, capsys)
    assert "Last backup Result FAILED" in output
    assert ("Diagnostic tail FAILED:" in output) == verbose
    assert (str(store.log_path(attempt_id)) in output) == verbose
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
    assert "Monitor: RUNNING" in output
    assert "Monitor updates are stale or unavailable" in output
    assert "Lock ownership does not confirm responsive observation" in output


def test_enabled_stopped_monitor_has_action(human_status_args, monkeypatch, capsys):
    _enable_trigger(human_status_args)
    monkeypatch.setattr(
        "vaultkeep.trigger.cli._monitor_runtime_status", lambda _scope: "not running"
    )
    output, _ = _output(human_status_args, capsys)
    assert "Monitoring is not confirmed running" in output
    assert "Application shutdowns may not trigger backups" in output
    assert "Action Check monitor service and journal" in output


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
    assert "Result SUCCESS (source consistency uncertain)" in output
    assert "Completed 2026-10-22 09:35:00 UTC" in output
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
    assert "Backup UNKNOWN" in output
    assert "Backup dispatch blocked by recovery state" in output


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
    assert ("Submission rejections" in output) == pending
    assert ("Backup PENDING" in output) == pending
