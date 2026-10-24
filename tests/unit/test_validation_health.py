"""Validation before setup and health checks against disposable job state."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace

import pytest

from vaultkeep.cli import main, validation
from vaultkeep.schedule import TimerManager, TimerPaths
from vaultkeep.state.identity import job_identity_hash, job_state_path
from vaultkeep.state.models import LastRunState, LastSuccessfulBackup, LocalState
from vaultkeep.trigger.config import load_config
from vaultkeep.trigger.errors import BlockedError
from vaultkeep.trigger.models import InstanceState, Observation, ObservationRecord, utc_now
from vaultkeep.trigger.state import StateStore


def report_text(text):
    """Normalize alignment for health assertions; rendering has separate exact tests."""
    text = re.sub(r"(?m)^  (\S.*?) {2,}", r"\1: ", text)
    return re.sub(r"(?m)^(Job|Mode|Result): +", r"\1: ", text)


@pytest.fixture
def health_job(tmp_path, valid_config, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    valid_config["sources"][0]["path"] = str(source)
    valid_config["destination"].update(root=str(destination))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(validation, "state_root", lambda _scope: tmp_path / "trigger-state")
    monkeypatch.setattr(validation.os, "geteuid", lambda: 1000)
    return tmp_path / "app.yaml", valid_config


def write_job(health_job, *, trigger=False, schedule=False):
    path, data = health_job
    data["schedule"]["enabled"] = schedule
    if trigger:
        data["trigger"] = {
            "enabled": True,
            "app": "bisq",
            "data_dir": str(path.parent / "bisq"),
            "matcher": {"entrypoints": [{"kind": "java_main", "value": "bisq.Main"}]},
        }
    path.write_text(json.dumps(data))
    return path


def properties(**updates):
    return {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "UnitFileState": "enabled",
        "NextElapseUSecRealtime": "Wed 2026-10-21 23:00:00 EEST",
        "Result": "success",
        **updates,
    }


def trigger_state(path, monkeypatch, **updates):
    config = load_config(path)
    state = InstanceState(
        instance_id=config.job_id,
        config_fingerprint=config.instances[0].fingerprint(),
        last_monitor_update=utc_now(),
        last_observation=ObservationRecord(observation=Observation.STOPPED, observed_at=utc_now()),
    )
    store = StateStore(path.parent / "trigger-state")
    store.ensure()
    store.write_instance(replace(state, **updates))
    monkeypatch.setattr(validation, "runtime_preflight", lambda *args, **kwargs: "test-version")
    monkeypatch.setattr(validation, "_unit_properties", lambda *args, **kwargs: properties())
    return store


def test_missing_monitor_reports_incomplete_and_checks_remaining_state(
    health_job, monkeypatch, capsys
):
    path = write_job(health_job, trigger=True)
    calls = []
    monkeypatch.setattr(
        validation, "runtime_preflight", lambda *args, **kwargs: calls.append(kwargs)
    )
    monkeypatch.setattr(
        validation, "_unit_properties", lambda *args, **kwargs: {"LoadState": "not-found"}
    )
    assert main(["--user", "--config", str(path), "validate"]) != 0
    output = report_text(capsys.readouterr().out)
    assert "Trigger: ENABLED" in output and "Schedule:" not in output
    assert "Backup paths: OK" in output
    assert "Trigger runtime prerequisites: OK" in output
    assert "Monitor service: NOT INSTALLED" in output
    assert "Monitor updates: no recorded state" in output
    assert "Result: SETUP INCOMPLETE" in output
    assert len(calls) == 1 and calls[0]["validate_backup"] is False
    assert callable(calls[0]["warning"])
    assert not (path.parent / "trigger-state").exists()


def test_healthy_trigger(health_job, monkeypatch, capsys):
    path = write_job(health_job, trigger=True)
    trigger_state(path, monkeypatch)
    assert main(["--user", "--config", str(path), "validate"]) == 0
    output = report_text(capsys.readouterr().out)
    assert "Monitor service: RUNNING" in output
    assert "Monitor updates: current" in output
    assert "Result: OK" in output


@pytest.mark.parametrize(
    "updates, message",
    [
        ({"last_monitor_update": "2000-01-01T00:00:00Z"}, "Monitor updates: stale"),
        ({"config_fingerprint": "old"}, "Monitor configuration: differs"),
        ({"review_required": True, "review_reason": "gap"}, "review required — gap"),
    ],
)
def test_trigger_state_health_failures(health_job, monkeypatch, capsys, updates, message):
    path = write_job(health_job, trigger=True)
    trigger_state(path, monkeypatch, **updates)
    assert main(["--user", "--config", str(path), "validate"]) != 0
    output = report_text(capsys.readouterr().out)
    assert message in output
    assert "Result: OK" not in output


@pytest.mark.parametrize("active, enabled", [("failed", "enabled"), ("inactive", "disabled")])
def test_trigger_service_failure(health_job, monkeypatch, capsys, active, enabled):
    path = write_job(health_job, trigger=True)
    trigger_state(path, monkeypatch)
    monkeypatch.setattr(
        validation,
        "_unit_properties",
        lambda *args, **kwargs: properties(ActiveState=active, UnitFileState=enabled),
    )
    assert main(["--user", "--config", str(path), "validate"]) != 0
    assert "Result: FAILED" in report_text(capsys.readouterr().out)


def test_failed_prerequisites_do_not_hide_missing_monitor(health_job, monkeypatch, capsys):
    path = write_job(health_job, trigger=True)
    trigger_state(path, monkeypatch)

    def unavailable(*args, **kwargs):
        raise BlockedError("worker template is missing")

    monkeypatch.setattr(validation, "runtime_preflight", unavailable)
    monkeypatch.setattr(
        validation, "_unit_properties", lambda *args, **kwargs: {"LoadState": "not-found"}
    )
    assert main(["--user", "--config", str(path), "validate"]) != 0
    output = capsys.readouterr()
    output_out = report_text(output.out)
    assert "worker template is missing" in output.err
    assert "Monitor service: NOT INSTALLED" in output_out


@pytest.mark.parametrize("user_mode, uid", [(False, 1000), (False, 0), (True, 0)])
def test_trigger_rejects_system_mode_or_root(health_job, monkeypatch, capsys, user_mode, uid):
    path = write_job(health_job, trigger=True)
    monkeypatch.setattr(validation.os, "geteuid", lambda: uid)

    def unexpected(*args, **kwargs):
        pytest.fail("unsupported mode must not query user services")

    monkeypatch.setattr(validation, "runtime_preflight", unexpected)
    monkeypatch.setattr(validation, "_unit_properties", unexpected)
    assert main([*(["--user"] if user_mode else []), "--config", str(path), "validate"]) != 0
    assert "unsupported" in report_text(capsys.readouterr().out)


def test_schema_only_skips_all_automation_checks(health_job, monkeypatch, capsys):
    path = write_job(health_job, trigger=True)

    def unexpected(*args, **kwargs):
        pytest.fail("schema-only validation must not inspect runtime or services")

    monkeypatch.setattr(validation, "_unit_properties", unexpected)
    monkeypatch.setattr(validation, "validate_job", unexpected)
    monkeypatch.setattr(validation, "runtime_preflight", unexpected)
    assert main(["--config", str(path), "validate", "--schema-only"]) == 0
    assert "Runtime checks: skipped" in report_text(capsys.readouterr().out)


def test_backup_preflight_does_not_reenter_automation_health(health_job, monkeypatch, capsys):
    path = write_job(health_job, trigger=True)

    def unexpected(*args, **kwargs):
        pytest.fail("internal backup validation must not inspect automation")

    monkeypatch.setattr(validation, "runtime_preflight", unexpected)
    assert main(["--user", "--config", str(path), "validate", "--backup-only"]) == 0
    assert report_text(capsys.readouterr().out).strip() == "validate: valid"


@pytest.mark.parametrize("user_mode", [False, True])
@pytest.mark.parametrize("installed", [False, True])
def test_schedule_checks_selected_manager_and_installation(
    health_job, monkeypatch, capsys, user_mode, installed
):
    path = write_job(health_job, schedule=True)
    paths = TimerPaths(path.parent, path.parent / "units", path.parent / "registry.json")
    manager = TimerManager(paths, user_mode=user_mode)
    calls = []
    monkeypatch.setattr(manager, "require_environment", lambda: calls.append("environment"))
    monkeypatch.setattr(manager, "_run", lambda command, **kwargs: calls.append(command) or "")

    def selected_manager(*, user_mode):
        assert user_mode == manager.user_mode
        return manager

    monkeypatch.setattr(validation, "TimerManager", selected_manager)

    def inspect(unit, *, user_mode):
        assert user_mode == manager.user_mode
        calls.append(unit)
        if not installed:
            return {"LoadState": "not-found"}
        return (
            properties()
            if unit.endswith("timer")
            else properties(ActiveState="inactive", SubState="dead")
        )

    monkeypatch.setattr(validation, "_unit_properties", inspect)
    if installed:
        timer = manager._timer_drop_in_path("app")
        timer.parent.mkdir(parents=True)
        timer.write_text("timer")
        if user_mode:
            manager._service_drop_in_path("app").write_text("service")
    before = sorted(path.parent.rglob("*"))
    code = main([*(["--user"] if user_mode else []), "--config", str(path), "validate"])
    assert (code == 0) == installed
    output = report_text(capsys.readouterr().out)
    assert "Schedule: ENABLED" in output and "Trigger:" not in output
    assert "Schedule runtime prerequisites: OK" in output
    assert ("Next scheduled run:" in output) if installed else ("Timer: NOT INSTALLED" in output)
    assert "vaultkeep@app.timer" in calls
    assert "vaultkeep@app.service" in calls
    assert before == sorted(path.parent.rglob("*"))


def test_trigger_params_defaults_are_shared_by_validation_and_service_identity(
    health_job, monkeypatch
):
    path = write_job(health_job, trigger=True)
    data = json.loads(path.read_text())
    data["destination"]["root"] += "/{NETWORK}"
    data["trigger"]["params"] = {"NETWORK": "testnet"}
    path.write_text(json.dumps(data))
    (path.parent / "destination" / "testnet").mkdir()
    trigger_state(path, monkeypatch)
    assert main(["--user", "--config", str(path), "validate"]) == 0


def test_recorded_backup_failure_is_unhealthy(health_job, capsys):
    path = write_job(health_job)
    state_path = job_state_path(path, "app", state_root=path.parent / "state/vaultkeep/jobs")
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        LocalState(
            job_id="app",
            job_identity_hash=job_identity_hash(path, "app"),
            last_run=LastRunState(at_utc=utc_now(), result="failed"),
            application_version="test",
        ).model_dump_json()
    )
    assert main(["--user", "--config", str(path), "validate"]) != 0
    assert "Last result: failed" in report_text(capsys.readouterr().out)


@pytest.mark.parametrize("stdout,code", [("", 1), ("LoadState=loaded\n", 0)])
def test_incomplete_systemd_response_is_unknown(monkeypatch, stdout, code):
    def inspect(command, **kwargs):
        assert command[:2] == ["systemctl", "--user"]
        assert "show" in command and kwargs["timeout"] == 15
        return subprocess.CompletedProcess(command, code, stdout, "bus unavailable")

    monkeypatch.setattr("vaultkeep.schedule.status.subprocess.run", inspect)
    with pytest.raises(validation.TimerError, match="cannot inspect"):
        validation._unit_properties("vaultkeep@test.timer", user_mode=True)


def test_real_preflight_child_accepts_enabled_trigger_before_monitor_installation(health_job):
    from vaultkeep.trigger.preflight import validate_jobs

    path = write_job(health_job, trigger=True)
    validate_jobs(load_config(path))
    assert not (path.parent / "trigger-state").exists()


def test_source_failure_still_reports_automation_installation(health_job, monkeypatch, capsys):
    path = write_job(health_job, trigger=True)
    (path.parent / "source").rmdir()
    monkeypatch.setattr(validation, "runtime_preflight", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        validation, "_unit_properties", lambda *args, **kwargs: {"LoadState": "not-found"}
    )
    assert main(["--user", "--config", str(path), "validate"]) == 5
    output = capsys.readouterr()
    output_out = report_text(output.out)
    assert "Configured source does not exist" in output.err
    assert "Monitor service: NOT INSTALLED" in output_out


@pytest.mark.parametrize(
    "updates, expected",
    [
        ({"last_observation": None}, "Process observation: unavailable"),
        (
            {
                "last_observation": ObservationRecord(
                    observation=Observation.AMBIGUOUS,
                    observed_at=utc_now(),
                    reason="multiple candidates",
                )
            },
            "Process observation: ambiguous",
        ),
        (
            {
                "attempt_summaries": [
                    {"attempt_id": "attempt", "request_id": "request", "outcome": "backup_failed"}
                ]
            },
            "Last dispatch: backup_failed",
        ),
    ],
)
def test_ambiguous_observation_and_recorded_failure_are_unhealthy(
    health_job, monkeypatch, capsys, updates, expected
):
    path = write_job(health_job, trigger=True)
    trigger_state(path, monkeypatch, **updates)
    assert main(["--user", "--config", str(path), "validate"]) != 0
    assert expected in report_text(capsys.readouterr().out)


def test_corrupt_trigger_state_is_reported_without_repair(health_job, monkeypatch, capsys):
    path = write_job(health_job, trigger=True)
    store = trigger_state(path, monkeypatch)
    state_path = store.root / "instances/app.json"
    state_path.write_text("{broken")
    assert main(["--user", "--config", str(path), "validate"]) == 15
    output = capsys.readouterr()
    output_out = report_text(output.out)
    assert "Trigger recovery checks: FAILED" in output_out
    assert state_path.read_text() == "{broken"


def test_per_file_failure_uses_file_identity_and_readable_name(health_job, capsys):
    from vaultkeep.file_backups import file_key

    path = write_job(health_job)
    data = json.loads(path.read_text())
    data["backup_mode"] = "per_file"
    path.write_text(json.dumps(data))
    (path.parent / "source/wallet.dat").write_text("wallet")
    key = file_key("wallet.dat")
    state_path = job_state_path(
        path, "app", file_key=key, state_root=path.parent / "state/vaultkeep/jobs"
    )
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        LocalState(
            job_id="app",
            job_identity_hash=job_identity_hash(path, "app", file_key=key),
            last_run=LastRunState(at_utc=utc_now(), result="failed"),
            application_version="test",
        ).model_dump_json()
    )
    assert main(["--user", "--config", str(path), "validate"]) != 0
    assert "Last result (wallet.dat): failed" in report_text(capsys.readouterr().out)


@pytest.mark.parametrize("user_mode", [False, True])
@pytest.mark.parametrize("problem", ["no-next-run", "failed-backup", "disabled", "masked"])
def test_schedule_health_failures(health_job, monkeypatch, capsys, user_mode, problem):
    path = write_job(health_job, schedule=True)
    manager = TimerManager(TimerPaths(path.parent, path.parent / "units"), user_mode=user_mode)
    monkeypatch.setattr(manager, "require_environment", lambda: None)
    monkeypatch.setattr(manager, "_run", lambda *args, **kwargs: "")
    monkeypatch.setattr(validation, "TimerManager", lambda **kwargs: manager)
    timer = manager._timer_drop_in_path("app")
    timer.parent.mkdir(parents=True)
    timer.write_text("timer")
    if user_mode:
        manager._service_drop_in_path("app").write_text("service")

    def inspect(unit, *, user_mode):
        if unit.endswith(".service"):
            return properties(Result="exit-code" if problem == "failed-backup" else "success")
        if problem == "no-next-run":
            return properties(NextElapseUSecRealtime="")
        if problem == "disabled":
            return properties(UnitFileState="disabled")
        if problem == "masked":
            return properties(LoadState="masked", UnitFileState="masked")
        return properties()

    monkeypatch.setattr(validation, "_unit_properties", inspect)
    assert main([*(["--user"] if user_mode else []), "--config", str(path), "validate"]) != 0
    assert "Result: FAILED" in report_text(capsys.readouterr().out)


@pytest.mark.parametrize("level", ["error", "warning", "info", "debug"])
def test_report_survives_log_level_and_suppresses_secondary_missing_state_error(
    health_job, monkeypatch, capsys, level
):
    path = write_job(health_job, trigger=True)
    data = json.loads(path.read_text())
    data["logging"]["level"] = level
    path.write_text(json.dumps(data))
    monkeypatch.setattr(validation, "runtime_preflight", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        validation, "_unit_properties", lambda *args, **kwargs: {"LoadState": "not-found"}
    )

    assert main(["--user", "--config", str(path), "validate"]) == 3
    output = capsys.readouterr()
    assert output.out.startswith(
        "Vaultkeep — Validation\n\nJob:     app\nMode:    user\nResult:  SETUP INCOMPLETE\n"
    )
    assert output.out.index("Result:") < output.out.index("\nConfiguration\n")
    assert "Configuration  VALID" in output.out
    assert "no completed run recorded" in output.out
    assert "no recorded state" in output.out
    assert output.out.count("Issue:") == 1
    assert "Monitor service is not installed; automatic backups are unavailable." in output.out
    assert "trigger install" in output.out
    assert "INFO:" not in output.out and "ERROR:" not in output.out
    assert ("Monitor service unit: vaultkeep-trigger@" in output.err) == (
        level in ("info", "debug")
    )
    assert ("DEBUG:" in output.err and "Checking" in output.err) == (level == "debug")
    assert "no recorded state" not in output.err
    assert "\033" not in output.out + output.err


def test_backup_timestamp_and_result_are_separate_fields(health_job, capsys):
    path = write_job(health_job)
    state_path = job_state_path(path, "app", state_root=path.parent / "state/vaultkeep/jobs")
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        LocalState(
            job_id="app",
            job_identity_hash=job_identity_hash(path, "app"),
            last_run=LastRunState(at_utc="2026-10-21T19:29:29Z", result="unchanged"),
            last_unchanged_at_utc="2026-10-21T19:29:29Z",
            last_successful_backup=LastSuccessfulBackup(
                source_digest="sha256:" + "a" * 64,
                config_fingerprint="sha256:" + "b" * 64,
                backup_id="c" * 32,
                created_at_utc="2026-10-20T19:29:29Z",
                backup_path="/backup/archive.tar.zst",
                application_version="test",
            ),
            application_version="test",
        ).model_dump_json()
    )

    assert main(["--user", "--config", str(path), "validate"]) == 0
    output = capsys.readouterr().out
    assert (
        "  Last recorded run  2026-10-21 19:29:29 UTC\n  Last result        unchanged\n" in output
    )


def test_preflight_warning_is_visible_at_error_log_level(health_job, monkeypatch, capsys):
    path = write_job(health_job, trigger=True)
    data = json.loads(path.read_text())
    data["logging"]["level"] = "error"
    path.write_text(json.dumps(data))
    trigger_state(path, monkeypatch)

    def prerequisites(*args, warning, **kwargs):
        warning("configured Vaultkeep executable version differs from this installation test")

    monkeypatch.setattr(validation, "runtime_preflight", prerequisites)
    assert main(["--user", "--config", str(path), "validate"]) == 0
    output = capsys.readouterr()
    assert "WARNING" in output.out
    assert output.out.count("executable version differs") == 1
    assert output.err == ""
