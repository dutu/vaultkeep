from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.trigger.conftest import (
    FakeController,
    FakeProvider,
    RecordingNotifier,
    make_config,
    make_instance,
    make_request,
)
from tests.trigger.test_monitor import _running_proc
from vaultkeep.cli import main
from vaultkeep.trigger.models import (
    AttemptOutcome,
    InstanceState,
    LaunchRecord,
    Observation,
    ObservationRecord,
    Receipt,
    utc_now,
)
from vaultkeep.trigger.monitor import Monitor, _Budget, unit_name_for
from vaultkeep.trigger.state import StateStore
from vaultkeep.trigger.systemd import UnitState


def _monitor(store: StateStore, *instances):
    provider = FakeProvider([_running_proc()])
    controller = FakeController()
    notifier = RecordingNotifier()
    monitor = Monitor(
        make_config(*instances),
        store=store,
        provider=provider,
        controller=controller,
        notifier=notifier,
        clock=lambda: 0.0,
    )
    return monitor, provider, controller, notifier


@pytest.mark.parametrize("phase", ["startup", "tick"])
@pytest.mark.parametrize("fault", ["missing", "contradictory", "corrupt", "schema", "encoding"])
def test_bad_instance_is_blocked_without_stopping_healthy_instance(
    store: StateStore, phase: str, fault: str
) -> None:
    broken = make_instance(instance_id="broken", data_dir="/home/user/.broken", confirm_seconds=0)
    healthy = make_instance(instance_id="healthy", confirm_seconds=0)
    request = make_request(broken)
    state = InstanceState(
        instance_id=broken.id,
        active_attempt_id=request.attempt_id,
        active_request_id=request.request_id,
    )
    store.write_instance(state)
    if fault == "contradictory":
        store.write_request(replace(request, instance_id="wrong-owner"))
    elif fault == "corrupt":
        store.instance_path(broken.id).write_text("{not-json")
    elif fault == "encoding":
        store.instance_path(broken.id).write_bytes(b"\xff\xfe")
    elif fault == "schema":
        data = state.to_dict()
        data["schema_version"] = 999
        store.instance_path(broken.id).write_text(json.dumps(data))
    original = store.instance_path(broken.id).read_bytes()
    monitor, provider, controller, _notifier = _monitor(store, broken, healthy)
    if phase == "startup":
        monitor._startup_reconcile()
    monitor.run_once()
    assert store.read_blocked(broken.id)
    assert store.instance_path(broken.id).read_bytes() == original
    running = store.read_instance(healthy.id)
    assert running is not None and running.armed
    provider.processes = []
    monitor.run_once()
    monitor.run_once()
    assert len(controller.started) == 1
    working = store.read_instance(healthy.id)
    assert working is not None and working.active_attempt_id is not None
    assert controller.started[0] == unit_name_for(working.active_attempt_id)
    restarted, _provider, _controller, _notifier = _monitor(store, broken, healthy)
    restarted._startup_reconcile()
    assert store.read_blocked(broken.id)
    assert store.instance_path(broken.id).read_bytes() == original


@pytest.mark.parametrize("phase", ["startup", "tick"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active_attempt_id", []),
        ("active_attempt_id", 123),
        ("blocking_attempt_id", {}),
        ("armed", "yes"),
        ("last_observation", []),
        ("pending_request", "not-a-request"),
        ("notification_markers", "not-an-array"),
        (
            "attempt_summaries",
            [{"attempt_id": "a", "request_id": "r", "outcome": []}],
        ),
    ],
)
def test_malformed_instance_field_is_blocked_without_stopping_healthy_instance(
    store: StateStore, phase: str, field: str, value: object
) -> None:
    broken = make_instance(instance_id="broken", data_dir="/home/user/.broken")
    healthy = make_instance(instance_id="healthy", confirm_seconds=0)
    data = InstanceState(instance_id=broken.id).to_dict()
    data[field] = value
    store.instance_path(broken.id).write_text(json.dumps(data))
    original = store.instance_path(broken.id).read_bytes()

    monitor, provider, controller, _notifier = _monitor(store, broken, healthy)
    if phase == "startup":
        monitor._startup_reconcile()
    monitor.run_once()

    assert store.read_blocked(broken.id)
    assert store.instance_path(broken.id).read_bytes() == original
    running = store.read_instance(healthy.id)
    assert running is not None and running.armed

    provider.processes = []
    monitor.run_once()
    monitor.run_once()
    assert len(controller.started) == 1
    working = store.read_instance(healthy.id)
    assert working is not None and working.active_attempt_id is not None


@pytest.mark.parametrize("phase", ["startup", "tick"])
@pytest.mark.parametrize(
    "value",
    [10**400, -(10**400), float("inf"), float("-inf"), float("nan")],
    ids=["overflow", "negative-overflow", "infinity", "negative-infinity", "nan"],
)
def test_invalid_number_blocks_only_affected_instance(
    store: StateStore, phase: str, value: int | float
) -> None:
    broken = make_instance(instance_id="broken", data_dir="/home/user/.broken")
    healthy = make_instance(instance_id="healthy", confirm_seconds=0)
    data = InstanceState(
        instance_id=broken.id,
        last_observation=ObservationRecord(Observation.RUNNING, utc_now()),
    ).to_dict()
    data["last_observation"]["create_time"] = value
    path = store.instance_path(broken.id)
    path.write_text(json.dumps(data))
    original = path.read_bytes()

    monitor, provider, controller, _notifier = _monitor(store, broken, healthy)
    if phase == "startup":
        monitor._startup_reconcile()
    monitor.tick(_Budget(next_due={}))

    reason = store.read_blocked(broken.id)
    assert reason is not None and "observation.create_time must be a finite number" in reason
    assert path.read_bytes() == original
    running = store.read_instance(healthy.id)
    assert running is not None and running.armed

    provider.processes = []
    monitor.run_once()
    monitor.run_once()
    working = store.read_instance(healthy.id)
    assert working is not None and working.active_attempt_id is not None
    assert controller.started == [unit_name_for(working.active_attempt_id)]

    restarted, _provider, _controller, _notifier = _monitor(store, broken, healthy)
    restarted._startup_reconcile()
    assert store.read_blocked(broken.id) == reason
    assert path.read_bytes() == original


def test_status_reports_durable_block_without_mutating_evidence(
    store: StateStore, monkeypatch, capsys, valid_config: dict
) -> None:
    store.write_instance(InstanceState(instance_id="broken", active_attempt_id="ghost"))
    store.write_blocked("broken", "active attempt ghost has no request")
    before = {path: path.read_bytes() for path in store.root.rglob("*.json")}
    monkeypatch.setattr("vaultkeep.trigger.status.state_root", lambda _scope: store.root)
    monkeypatch.setattr("vaultkeep.trigger.status._monitor_status", lambda _scope: "stopped")
    valid_config.update(
        job={"id": "broken"}, trigger={"enabled": False}, schedule={"enabled": False}
    )
    path = store.root.parent / "app.yaml"
    path.write_text(json.dumps(valid_config))
    assert main(["--user", "--config", str(path), "status"]) == 15
    assert "active attempt ghost has no request" in capsys.readouterr().out
    assert {path: path.read_bytes() for path in store.root.rglob("*.json")} == before


def test_status_reports_readable_instances_and_block_when_another_record_is_corrupt(
    store: StateStore,
    monkeypatch,
    capsys,
    valid_config: dict,
) -> None:
    store.write_instance(InstanceState(instance_id="broken"))
    store.instance_path("broken").write_text("{corrupt")
    store.write_blocked("broken", "corrupt instance state requires review")
    store.write_instance(InstanceState(instance_id="healthy", armed=True))
    before = {path: path.read_bytes() for path in store.root.rglob("*.json")}
    monkeypatch.setattr("vaultkeep.trigger.status.state_root", lambda _scope: store.root)
    monkeypatch.setattr("vaultkeep.trigger.status._monitor_status", lambda _scope: "running")
    valid_config.update(
        job={"id": "broken"}, trigger={"enabled": False}, schedule={"enabled": False}
    )
    path = store.root.parent / "app.yaml"
    path.write_text(json.dumps(valid_config))
    assert main(["--user", "--config", str(path), "status"]) == 15
    output = capsys.readouterr()
    assert "corrupt instance state requires review" in output.out
    assert "Application  UNKNOWN" in " ".join(output.out.split()) or "UNKNOWN" in output.out
    assert "healthy" not in output.out
    assert {path: path.read_bytes() for path in store.root.rglob("*.json")} == before


def test_persistence_failure_does_not_authorize_other_dispatch(
    store: StateStore, monkeypatch
) -> None:
    instance = make_instance()
    store.write_instance(InstanceState(instance_id=instance.id, active_attempt_id="ghost"))
    monitor, _provider, controller, _notifier = _monitor(store, instance)

    def unwritable(*_args):
        raise OSError("diagnostic cannot be persisted")

    monkeypatch.setattr(store, "write_blocked", unwritable)
    with pytest.raises(OSError, match="cannot be persisted"):
        monitor.run_once()
    assert not controller.started


def test_unreferenced_never_launched_orphans_are_summarized_and_pruned(store: StateStore) -> None:
    instance = make_instance(confirm_seconds=0)
    store.write_instance(InstanceState(instance_id=instance.id, latest_request_id=""))
    for _ in range(71):
        store.write_request(make_request(instance))
    monitor, _provider, controller, notifier = _monitor(store, instance)
    state = monitor._load_state(instance.id)
    assert not monitor._history_bound_exceeded(state, instance)
    monitor._startup_reconcile()
    state = store.read_instance(instance.id)
    assert state is not None and state.active_attempt_id is None and state.pending_request is None
    assert len(state.attempt_summaries) == 71
    assert len(store.list_attempt_ids()) <= 20
    assert not controller.started and not notifier.calls
    assert store.read_blocked(instance.id) is None


def test_matching_pending_orphan_is_recovered_without_replaying_obsolete_work(
    store: StateStore,
) -> None:
    instance = make_instance(confirm_seconds=0)
    request = make_request(instance)
    store.write_request(request)
    store.write_instance(
        InstanceState(
            instance_id=instance.id,
            pending_request=replace(request, attempt_id=""),
            latest_request_id=request.request_id,
        )
    )
    monitor, _provider, controller, _notifier = _monitor(store, instance)
    monitor._startup_reconcile()
    state = store.read_instance(instance.id)
    assert state is not None and state.pending_request is not None
    assert state.pending_request.request_id == request.request_id
    assert state.retry_not_before is not None and state.pending_reconfirmation_required
    assert not controller.started


def test_launched_orphan_holds_slot_and_preserves_newer_pending(store: StateStore) -> None:
    instance = make_instance(confirm_seconds=0)
    old = make_request(instance)
    newer = replace(make_request(instance), attempt_id="")
    store.write_request(old)
    store.write_launch(LaunchRecord(old.attempt_id, unit_name_for(old.attempt_id), utc_now()))
    store.write_instance(
        InstanceState(
            instance_id=instance.id, pending_request=newer, latest_request_id=newer.request_id
        )
    )
    monitor, _provider, controller, _notifier = _monitor(store, instance)
    controller.states[unit_name_for(old.attempt_id)] = UnitState("loaded", "activating", "start")
    monitor._startup_reconcile()
    state = store.read_instance(instance.id)
    assert state is not None and state.active_attempt_id == old.attempt_id
    assert (
        state.pending_request is not None and state.pending_request.request_id == newer.request_id
    )
    assert state.review_required
    assert store.request_path(old.attempt_id).exists() and not controller.started


def test_receipt_adoption_preserves_monitor_gap_evidence(store: StateStore) -> None:
    instance = make_instance()
    request = make_request(instance)
    store.write_request(request)
    store.write_launch(
        LaunchRecord(request.attempt_id, unit_name_for(request.attempt_id), utc_now())
    )
    store.write_receipt(
        Receipt(
            request.attempt_id,
            request.request_id,
            instance.id,
            AttemptOutcome.CREATED,
            utc_now(),
            utc_now(),
            1,
            exit_code=0,
        )
    )
    store.write_instance(
        InstanceState(instance_id=instance.id, last_monitor_update="2020-01-01T00:00:00Z")
    )
    # An additional orphan must not refresh the observation timestamp before gap detection.
    store.write_request(make_request(instance))
    monitor, _provider, controller, notifier = _monitor(store, instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor._startup_reconcile()
    state = store.read_instance(instance.id)
    assert state is not None and state.review_required
    assert any("Review required" in title for title, _body in notifier.calls)


def test_notification_markers_follow_retention_without_rearming_delivery(store: StateStore) -> None:
    instance = make_instance(confirm_seconds=0)
    monitor, provider, controller, notifier = _monitor(store, instance)
    expired_scopes: set[str] = set()
    for index in range(80):
        provider.processes = [_running_proc()]
        monitor.run_once()
        provider.processes = []
        monitor.run_once()
        monitor.run_once()
        state = store.read_instance(instance.id)
        assert (
            state is not None
            and state.active_attempt_id is not None
            and state.active_request_id is not None
        )
        if index == 0:
            expired_scopes.update((state.active_attempt_id, state.active_request_id))
        store.write_receipt(
            Receipt(
                state.active_attempt_id,
                state.active_request_id,
                instance.id,
                AttemptOutcome.CREATED,
                utc_now(),
                utc_now(),
                1,
                exit_code=0,
            )
        )
        controller.states[unit_name_for(state.active_attempt_id)] = UnitState(
            "loaded", "inactive", "dead"
        )
        monitor.run_once()
    state = store.read_instance(instance.id)
    assert state is not None
    assert len(store.list_attempt_ids()) == 50 and len(state.notification_markers) == 50
    assert not any(
        marker.partition(":")[0] in expired_scopes for marker in state.notification_markers
    )
    assert len([title for title, _body in notifier.calls if "requested" in title]) == 80
    monitor._startup_reconcile()
    assert len([title for title, _body in notifier.calls if "requested" in title]) == 80
