"""Trigger contributes monitoring and dispatch evidence to unified job status."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tests.trigger.conftest import make_request
from vaultkeep.cli import main
from vaultkeep.cli.status import collect_status
from vaultkeep.trigger.config import load_config
from vaultkeep.trigger.models import (
    AttemptOutcome,
    InstanceState,
    LaunchRecord,
    Observation,
    ObservationRecord,
    Receipt,
    utc_now,
)
from vaultkeep.trigger.status import _monitor_started_at, _monitor_status
from vaultkeep.trigger.systemd import UnitState


@pytest.fixture
def selected_job(tmp_path, valid_config, store, monkeypatch):
    valid_config["trigger"] = {
        "enabled": True,
        "app": "electrum",
        "data_dir": str(tmp_path / "wallet"),
        "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/electrum/run"}]},
    }
    valid_config["schedule"] = {"enabled": False}
    path = tmp_path / "job.yaml"
    path.write_text(json.dumps(valid_config))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "backup-state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr("vaultkeep.trigger.status.state_root", lambda _scope: store.root)
    monkeypatch.setattr("vaultkeep.trigger.status._monitor_status", lambda _scope: "running")
    session = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    monkeypatch.setattr("vaultkeep.trigger.status._monitor_started_at", lambda _scope: session)
    monkeypatch.setattr(
        "vaultkeep.trigger.status.RealSystemdController.unit_state", lambda *_: None
    )
    config = load_config(path)
    instance = config.instances[0]
    state = InstanceState(
        instance_id="app",
        last_monitor_update=utc_now(),
        config_fingerprint=instance.fingerprint(),
        last_observation=ObservationRecord(Observation.RUNNING, utc_now()),
        armed=True,
    )
    store.write_instance(state)
    return path, config, state


def report(path):
    return collect_status(path, user_mode=True, runtime_params={})


@pytest.mark.parametrize(
    "stage,expected",
    [
        ("running", "RUNNING"),
        ("confirming", "CONFIRMING STOP"),
        ("stopped", "HAS STOPPED"),
        ("unseen", "NOT YET DETECTED"),
    ],
)
def test_application_detection_and_confirmed_shutdown(selected_job, store, stage, expected):
    path, config, state = selected_job
    if stage != "running":
        state.last_observation = ObservationRecord(Observation.STOPPED, utc_now())
    if stage == "confirming":
        state.candidate_since = state.last_observation.observed_at
    if stage == "stopped":
        request = make_request(config.instances[0])
        state.armed = False
        state.latest_request_id = request.request_id
        state.pending_request = replace(request, attempt_id="")
        state.last_observation = ObservationRecord(Observation.STOPPED, utc_now())
    if stage == "unseen":
        state.armed = False
    store.write_instance(state)
    data = report(path)
    assert data["trigger"]["application"] == expected
    assert data["trigger"]["monitor"] == "RUNNING"
    assert data["backup"]["activity"] == "IDLE"
    assert data["backup"]["previous"] is None


@pytest.mark.parametrize(
    "problem",
    ["stale", "old_session", "future", "stopped", "ambiguous", "unavailable", "changed_config"],
)
def test_unreliable_monitoring_cannot_claim_application_running(
    selected_job, store, monkeypatch, problem
):
    path, _config, state = selected_job
    if problem == "stale":
        state.last_monitor_update = "2000-01-01T00:00:00Z"
    elif problem == "future":
        state.last_monitor_update = "2999-01-01T00:00:00Z"
    elif problem == "old_session":
        state.last_observation = ObservationRecord(Observation.RUNNING, "2000-01-01T00:00:00Z")
    elif problem == "stopped":
        monkeypatch.setattr("vaultkeep.trigger.status._monitor_status", lambda _: "stopped")
    elif problem == "changed_config":
        state.config_fingerprint = "other"
    else:
        state.last_observation = ObservationRecord(
            Observation.AMBIGUOUS if problem == "ambiguous" else Observation.INSPECTION_UNAVAILABLE,
            utc_now(),
        )
    store.write_instance(state)
    assert report(path)["trigger"]["application"] == "UNKNOWN"


@pytest.mark.parametrize(
    "worker,expected",
    [
        (UnitState("loaded", "failed", "failed"), "FAILED"),
        (UnitState("not-found", "inactive", "dead"), "NOT INSTALLED"),
        (UnitState("loaded", "activating", "start"), "STARTING"),
    ],
)
def test_monitor_health_is_independent_of_backup_status(
    selected_job, monkeypatch, worker, expected
):
    path, _, _ = selected_job
    monkeypatch.setattr("vaultkeep.trigger.status._monitor_status", lambda _: "stopped")
    monkeypatch.setattr(
        "vaultkeep.trigger.status.RealSystemdController.unit_state", lambda *_: worker
    )
    data = report(path)
    assert data["trigger"]["monitor"] == expected
    assert data["backup"]["previous"] is None


@pytest.mark.parametrize(
    "outcome", [AttemptOutcome.BLOCKED, AttemptOutcome.DEFERRED, AttemptOutcome.OUTCOME_UNKNOWN]
)
def test_dispatch_outcomes_are_never_backup_results(selected_job, store, outcome):
    path, config, state = selected_job
    request = make_request(config.instances[0])
    store.write_request(request)
    state.attempt_summaries = [
        {
            "attempt_id": request.attempt_id,
            "request_id": request.request_id,
            "outcome": outcome.value,
        }
    ]
    store.write_instance(state)
    data = report(path)
    assert data["backup"]["previous"] is None
    assert data["trigger"]["diagnostics"]
    assert "previous" not in data["trigger"]


def test_unassociated_launch_is_dispatch_activity_without_running_backup(selected_job, store):
    path, config, state = selected_job
    request = make_request(config.instances[0])
    store.write_request(request)
    store.write_launch(LaunchRecord(request.attempt_id, "worker.service", utc_now()))
    result = report(path)
    assert result["trigger"]["dispatch"] == "ACTIVE"
    assert result["trigger"]["active_attempt_id"] == request.attempt_id
    assert result["backup"]["activity"] == "IDLE"
    assert store.read_instance("app") == state


def test_completed_receipt_does_not_reconcile_or_invent_backup_history(selected_job, store):
    path, config, state = selected_job
    request = make_request(config.instances[0])
    store.write_request(request)
    state.active_attempt_id = request.attempt_id
    state.active_request_id = request.request_id
    store.write_instance(state)
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
    result = report(path)
    assert result["trigger"]["dispatch"] == "AWAITING RECONCILIATION"
    assert result["backup"]["previous"] is None
    assert store.read_instance("app").active_attempt_id == request.attempt_id


@pytest.mark.parametrize("kind", ["state", "blocked", "attempt", "ownership"])
def test_corrupt_evidence_preserves_uncertainty_and_is_read_only(selected_job, store, kind):
    path, config, _state = selected_job
    if kind == "state":
        store.instance_path("app").write_text("{corrupt")
    elif kind == "blocked":
        store.blocked_dir.mkdir(exist_ok=True)
        store.blocked_path("app").write_text("{corrupt")
    else:
        request = make_request(config.instances[0])
        store.write_request(request)
        store.write_launch(LaunchRecord(request.attempt_id, "worker.service", utc_now()))
        if kind == "attempt":
            store.request_path(request.attempt_id).write_text("{corrupt")
        else:
            store.write_receipt(
                Receipt(
                    request.attempt_id,
                    "wrong-request",
                    "app",
                    AttemptOutcome.CREATED,
                    utc_now(),
                    utc_now(),
                    1.0,
                )
            )
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in store.root.rglob("*") if p.is_file()
    }
    result = report(path)
    assert result["trigger"]["dispatch"] == "UNKNOWN"
    assert result["trigger"]["read_error"]
    assert {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in store.root.rglob("*") if p.is_file()
    } == before


def test_human_and_json_use_the_same_interpretation(selected_job, capsys):
    path, _, _ = selected_job
    args = ["--user", "--config", str(path), "status"]
    assert main(args) == 0
    human = " ".join(capsys.readouterr().out.split())
    assert main([*args, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["trigger"]["application"] == "RUNNING"
    assert "Application RUNNING (observed" in human
    assert "Monitor RUNNING (updated" in human
    assert "Previous attempt" not in human and "Earlier unresolved" not in human
    assert "Schedule" not in human


def test_monitor_probe_and_session_timestamp_are_read_only(tmp_path, monkeypatch):
    import fcntl

    path = tmp_path / "monitor.lock"
    monkeypatch.setattr("vaultkeep.trigger.status.monitor_lock_path", lambda _: path)
    assert _monitor_status(None) == "stopped"

    assert not path.exists()
    with path.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = path.stat().st_mtime_ns
        assert _monitor_status(None) == "running"
        assert _monitor_started_at(None)
        assert path.stat().st_mtime_ns == before
    assert _monitor_status(None) == "stopped"


def test_parameter_defaults_and_overrides_select_the_same_backup_namespace(selected_job):
    from vaultkeep.backup.execution import Execution
    from vaultkeep.backup.paths import user_workflow_paths
    from vaultkeep.state.identity import job_identity_hash

    path, _, _ = selected_job
    raw = json.loads(path.read_text())
    raw["destination"]["root"] += "/{repo}"
    raw["trigger"]["params"] = {"repo": "one"}
    path.write_text(json.dumps(raw))
    identity = job_identity_hash(path, "app", runtime_params={"repo": "one"})
    with Execution(user_workflow_paths(), "app", identity) as active:
        active.complete("created")
    assert report(path)["backup"]["previous"]["result"] == "SUCCESS"
    other = collect_status(path, user_mode=True, runtime_params={"repo": "two"})
    assert other["backup"]["previous"] is None


@pytest.mark.parametrize("scheduled", [False, True])
def test_unused_trigger_placeholder_is_omitted_without_systemd_probes(
    selected_job, store, monkeypatch, scheduled
):
    path, _, _ = selected_job
    raw = json.loads(path.read_text())
    raw["trigger"] = {"enabled": False}
    raw["schedule"] = (
        {"enabled": True, "interval": "daily", "at": "21:00"} if scheduled else {"enabled": False}
    )
    path.write_text(json.dumps(raw))
    store.instance_path("app").unlink()
    monkeypatch.setattr("vaultkeep.trigger.status._monitor_status", lambda _: "stopped")
    monkeypatch.setattr(
        "vaultkeep.trigger.status.RealSystemdController.unit_state",
        lambda *_: pytest.fail("unused trigger cannot require a service query"),
    )
    monkeypatch.setattr(
        "vaultkeep.schedule.status.unit_properties",
        lambda *_args, **_kwargs: {"LoadState": "not-found"},
    )
    result = report(path)
    assert "trigger" not in result
    assert ("schedule" in result) == scheduled


def test_orphan_dispatch_evidence_is_visible_with_a_disabled_placeholder(selected_job, store):
    path, config, _ = selected_job
    request = make_request(config.instances[0])
    store.write_request(request)
    store.write_launch(LaunchRecord(request.attempt_id, "worker.service", utc_now()))
    store.instance_path("app").unlink()
    raw = json.loads(path.read_text())
    raw["trigger"] = {"enabled": False}
    path.write_text(json.dumps(raw))
    result = report(path)
    assert result["trigger"]["active_attempt_id"] == request.attempt_id
    assert result["trigger"]["dispatch"] == "ACTIVE"


def test_current_safety_warning_is_reported_once_and_not_summarized_as_history(
    selected_job, store, capsys
):
    path, config, state = selected_job
    request = make_request(config.instances[0])
    store.write_request(request)
    state.active_attempt_id = request.attempt_id
    state.active_request_id = request.request_id
    state.review_required = state.active_review_required = True
    state.attempt_summaries = [
        {
            "attempt_id": request.attempt_id,
            "request_id": request.request_id,
            "outcome": "created",
            "review_required": True,
        }
    ]
    store.write_instance(state)
    result = report(path)
    assert len(result["trigger"]["safety"]) == 1
    assert result["trigger"]["safety"][0]["attempt_id"] == request.attempt_id
    assert result["trigger"]["safety"][0]["current"] is True
    assert main(["--user", "--config", str(path), "status"]) == 0
    human = capsys.readouterr().out
    assert "Application stopped state could not be verified during backup execution" in human
    assert "previous attempts" not in human and "previous attempt)" not in human


def test_historical_safety_is_compact_with_read_only_verbose_and_json_details(
    selected_job, store, capsys
):
    path, config, state = selected_job
    state.review_required = True
    state.review_reason = "monitoring gap"
    attempts = [make_request(config.instances[0]) for _ in range(2)]
    for request in attempts:
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
        state.attempt_summaries.append(
            {
                "attempt_id": request.attempt_id,
                "request_id": request.request_id,
                "outcome": "created",
                "review_required": True,
            }
        )
    store.write_instance(state)
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.parent.rglob("*") if p.is_file()
    }
    args = ["--user", "--config", str(path), "status"]
    assert main(args) == 0
    human = " ".join(capsys.readouterr().out.split())
    assert "Activity IDLE" in human and "Result NONE" in human
    assert "Dispatch IDLE" in human
    assert "Safety REVIEW REQUIRED (2 previous attempts)" in human
    assert "Action Review affected backup executions" in human
    assert "Monitoring was interrupted" not in human and "Period" not in human
    assert all(request.attempt_id not in human for request in attempts)

    assert main([*args, "--verbose"]) == 0
    detailed = capsys.readouterr().out
    assert detailed.count("Monitoring was interrupted during possible backup execution") == 2
    assert detailed.count("Period") == 2
    assert all(request.attempt_id in detailed for request in attempts)
    assert "obtain a new backup with the app stopped" in detailed
    assert "Execution record" in detailed and "execution.json" in detailed

    for flags in (["--json"], ["--json", "--verbose"]):
        assert main([*args, *flags]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["backup"]["activity"] == "IDLE"
        assert data["backup"]["previous"] is None
        safety = data["trigger"]["safety"]
        assert len(safety) == 2 and all(item["current"] is False for item in safety)
        assert [item["attempt_id"] for item in safety] == [r.attempt_id for r in attempts]
        assert all(" — " in item["execution_period"] for item in safety)
        if "--verbose" in flags:
            assert data["trigger"]["details"]["state_directory"] == str(store.root)
            assert data["backup"]["details"]["execution_record"].endswith("execution.json")
    assert {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.parent.rglob("*") if p.is_file()
    } == before


def test_unidentified_historical_safety_does_not_invent_attempt_count(selected_job, store, capsys):
    path, _, state = selected_job
    state.review_required = True
    store.write_instance(state)
    assert main(["--user", "--config", str(path), "status"]) == 0
    human = " ".join(capsys.readouterr().out.split())
    assert "Safety REVIEW REQUIRED (previous executions)" in human


def test_missing_current_session_confirmation_uses_observation_wording(selected_job, store):
    path, _, state = selected_job
    state.last_observation = ObservationRecord(Observation.STOPPED, utc_now())
    state.armed = False
    state.latest_request_id = "unavailable-request"
    store.write_instance(state)
    trigger = report(path)["trigger"]
    assert trigger["application"] == "UNKNOWN"
    assert trigger["application_detail"] == " (current session observation unavailable)"
