from __future__ import annotations

import json
from pathlib import Path

import pytest

from vaultkeep.application import Observation
from vaultkeep.trigger.errors import StateError
from vaultkeep.trigger.models import (
    AttemptOutcome,
    InstanceState,
    LaunchRecord,
    ObservationRecord,
    Receipt,
    Request,
    new_uuid,
    utc_now,
)
from vaultkeep.trigger.state import StateStore
from vaultkeep.trigger.version import DIAGNOSTICS_CAP_BYTES


def _request(instance_id: str = "electrum-main") -> Request:
    now = utc_now()
    return Request(
        request_id=new_uuid(),
        attempt_id=new_uuid(),
        instance_id=instance_id,
        created_at=now,
        confirmed_at=now,
        boot_id="boot-1",
        config_fingerprint="fp",
        app="electrum",
        data_dir="/home/user/.electrum",
        matcher={"entrypoints": [{"kind": "script", "value": "/opt/app/run_app"}]},
        job_config="/jobs/app.yaml",
        job_params={},
        vaultkeep="/usr/local/bin/vaultkeep",
        environment={"HOME": "/home/user"},
        preflight_version=None,
        deadline_seconds=None,
        owner_uid=1000,
        execution_policy={},
    )


def test_instance_roundtrip(store: StateStore) -> None:
    state = InstanceState(instance_id="electrum-main")
    state.armed = True
    state.last_observation = ObservationRecord(
        observation=Observation.RUNNING, observed_at=utc_now()
    )
    store.write_instance(state)
    loaded = store.read_instance("electrum-main")
    assert loaded is not None
    assert loaded.armed is True
    assert loaded.last_observation.observation is Observation.RUNNING


def test_missing_instance_is_none(store: StateStore) -> None:
    assert store.read_instance("nope") is None


def test_schema_mismatch_raises(store: StateStore) -> None:
    path = store.instance_path("electrum-main")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 999, "instance_id": "electrum-main"}))
    with pytest.raises(StateError):
        store.read_instance("electrum-main")


def test_corrupt_json_raises(store: StateStore) -> None:
    path = store.instance_path("electrum-main")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    with pytest.raises(StateError):
        store.read_instance("electrum-main")


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
    ],
)
def test_malformed_instance_field_type_raises_state_error(
    store: StateStore, field: str, value: object
) -> None:
    data = InstanceState(instance_id="electrum-main").to_dict()
    data[field] = value
    path = store.instance_path("electrum-main")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    with pytest.raises(StateError, match="is malformed"):
        store.read_instance("electrum-main")


@pytest.mark.parametrize("record", ["instance", "request", "receipt"])
@pytest.mark.parametrize(
    "value",
    [10**400, -(10**400), float("inf"), float("-inf"), float("nan")],
    ids=["overflow", "negative-overflow", "infinity", "negative-infinity", "nan"],
)
def test_invalid_persisted_number_raises_state_error(
    store: StateStore, record: str, value: int | float
) -> None:
    request = _request()
    if record == "instance":
        data = InstanceState(
            instance_id=request.instance_id,
            last_observation=ObservationRecord(Observation.RUNNING, utc_now()),
        ).to_dict()
        data["last_observation"]["create_time"] = value
        path = store.instance_path(request.instance_id)
        read = store.read_instance
        record_id = request.instance_id
        field = "observation.create_time"
    elif record == "request":
        data = request.to_dict()
        data["deadline_seconds"] = value
        path = store.request_path(request.attempt_id)
        read = store.read_request
        record_id = request.attempt_id
        field = "request.deadline_seconds"
    else:
        data = Receipt(
            request.attempt_id,
            request.request_id,
            request.instance_id,
            AttemptOutcome.CREATED,
            utc_now(),
            utc_now(),
            1.0,
        ).to_dict()
        data["duration_seconds"] = value
        path = store.receipt_path(request.attempt_id)
        read = store.read_receipt
        record_id = request.attempt_id
        field = "receipt.duration_seconds"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    with pytest.raises(StateError) as error:
        read(record_id)
    assert str(path) in str(error.value)
    assert f"{field} must be a finite number" in str(error.value)


def test_request_is_immutable(store: StateStore) -> None:
    request = _request()
    store.write_request(request)
    with pytest.raises(StateError):
        store.write_request(request)
    assert store.read_request(request.attempt_id) == request


def test_receipt_is_immutable(store: StateStore) -> None:
    request = _request()
    store.write_request(request)
    receipt = Receipt(
        attempt_id=request.attempt_id,
        request_id=request.request_id,
        instance_id=request.instance_id,
        outcome=AttemptOutcome.CREATED,
        started_at=utc_now(),
        ended_at=utc_now(),
        duration_seconds=1.0,
    )
    store.write_receipt(receipt)
    store.write_receipt(receipt)  # no-op
    assert store.read_receipt(request.attempt_id).outcome is AttemptOutcome.CREATED


def test_diagnostics_truncation(store: StateStore) -> None:
    data = b"x" * (DIAGNOSTICS_CAP_BYTES + 100)
    ref, truncated = store.write_diagnostics("attempt-1", data)
    assert truncated is True
    assert Path(ref).stat().st_size == DIAGNOSTICS_CAP_BYTES


def test_launch_roundtrip(store: StateStore) -> None:
    launch = LaunchRecord(
        attempt_id="a", unit_name="vaultkeep-trigger-exec@a.service", dispatched_at=utc_now()
    )
    store.write_launch(launch)
    assert store.read_launch("a") == launch


def test_remove_attempt(store: StateStore) -> None:
    request = _request()
    store.write_request(request)
    assert store.attempt_dir(request.attempt_id).is_dir()
    store.remove_attempt(request.attempt_id)
    assert not store.attempt_dir(request.attempt_id).exists()


def test_blocked_diagnostic_is_versioned_and_preserves_first_evidence(store: StateStore) -> None:
    store.write_blocked("broken", "request missing")
    before = store.blocked_path("broken").read_bytes()
    store.write_blocked("broken", "replacement reason")
    assert store.read_blocked("broken") == "request missing"
    assert store.blocked_path("broken").read_bytes() == before
    assert store.list_blocked_ids() == ["broken"]
    data = json.loads(before)
    data["schema_version"] = 999
    store.blocked_path("broken").write_text(json.dumps(data))
    with pytest.raises(StateError, match="schema_version"):
        store.read_blocked("broken")
