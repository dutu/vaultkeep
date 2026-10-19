from __future__ import annotations

from dataclasses import replace

import pytest

from tests.conftest import make_instance, make_request
from vaultkeep_trigger.detection import DetectionResult
from vaultkeep_trigger.lifecycle import ExecutionContext, progress
from vaultkeep_trigger.models import InstanceState, Observation

CONTEXT = ExecutionContext(vaultkeep="/usr/local/bin/vaultkeep", environment={"HOME": "/home/user"})


def _detection(observation: Observation, boot: str = "boot-1") -> DetectionResult:
    return DetectionResult(observation=observation, boot_id=boot)


def test_cold_start_stopped_creates_no_request() -> None:
    state = InstanceState(instance_id="electrum-main")
    events = progress(
        state, make_instance(), _detection(Observation.STOPPED), CONTEXT, monotonic_now=0.0
    )
    assert state.armed is False
    assert state.pending_request is None
    assert [e.kind for e in events] == []


def test_running_arms() -> None:
    state = InstanceState(instance_id="electrum-main")
    progress(state, make_instance(), _detection(Observation.RUNNING), CONTEXT, monotonic_now=0.0)
    assert state.armed is True


def test_running_then_stopped_confirms() -> None:
    instance = make_instance(confirm_seconds=5.0)
    state = InstanceState(instance_id="electrum-main")
    progress(state, instance, _detection(Observation.RUNNING), CONTEXT, monotonic_now=0.0)

    first = progress(
        state,
        instance,
        _detection(Observation.STOPPED),
        CONTEXT,
        monotonic_now=1.0,
        utc_now_value="2026-01-01T00:00:00Z",
    )
    assert [e.kind for e in first] == ["candidate_started"]
    assert state.pending_request is None

    second = progress(
        state,
        instance,
        _detection(Observation.STOPPED),
        CONTEXT,
        monotonic_now=10.0,
        utc_now_value="2026-01-01T00:00:06Z",
    )
    assert [e.kind for e in second] == ["request_confirmed"]
    assert state.armed is False
    assert state.candidate_since is None
    assert state.pending_request is not None


def test_restart_clears_pending() -> None:
    instance = make_instance(confirm_seconds=0.0)
    state = InstanceState(instance_id="electrum-main")
    progress(state, instance, _detection(Observation.RUNNING), CONTEXT, monotonic_now=0.0)
    progress(state, instance, _detection(Observation.STOPPED), CONTEXT, monotonic_now=1.0)
    progress(state, instance, _detection(Observation.STOPPED), CONTEXT, monotonic_now=2.0)
    assert state.pending_request is not None

    progress(state, instance, _detection(Observation.RUNNING), CONTEXT, monotonic_now=3.0)
    assert state.pending_request is None


def test_ambiguity_clears_candidate() -> None:
    instance = make_instance(confirm_seconds=5.0)
    state = InstanceState(instance_id="electrum-main")
    progress(state, instance, _detection(Observation.RUNNING), CONTEXT, monotonic_now=0.0)
    progress(
        state,
        instance,
        _detection(Observation.STOPPED),
        CONTEXT,
        monotonic_now=1.0,
        utc_now_value="2026-01-01T00:00:00Z",
    )
    assert state.candidate_since is not None
    progress(
        state,
        instance,
        _detection(Observation.AMBIGUOUS),
        CONTEXT,
        monotonic_now=2.0,
        utc_now_value="2026-01-01T00:00:01Z",
    )
    assert state.candidate_since is None


def test_boot_change_invalidates_running_evidence() -> None:
    instance = make_instance(confirm_seconds=0.0)
    state = InstanceState(instance_id="electrum-main")
    progress(state, instance, _detection(Observation.RUNNING, "boot-1"), CONTEXT, monotonic_now=0.0)
    assert state.boot_id == "boot-1"
    events = progress(
        state,
        instance,
        _detection(Observation.STOPPED, "boot-2"),
        CONTEXT,
        monotonic_now=1.0,
    )
    assert "boot_changed" in [e.kind for e in events]
    assert state.armed is False
    assert state.pending_request is None


def test_supersession_counts_drops() -> None:
    instance = make_instance(confirm_seconds=0.0)
    state = InstanceState(instance_id="electrum-main")
    # A pending request that could not yet dispatch, with a fresh confirmed shutdown,
    # is superseded by the latest request and the drop is counted.
    from vaultkeep_trigger.models import Request

    state.pending_request = Request.from_dict(_dummy_request())
    state.armed = True
    state.candidate_since = "2026-01-01T00:00:00Z"
    state.candidate_monotonic = 0.0
    state.boot_id = "boot-1"
    state.config_fingerprint = instance.fingerprint()

    events = progress(
        state,
        instance,
        _detection(Observation.STOPPED, "boot-1"),
        CONTEXT,
        monotonic_now=10.0,
        utc_now_value="2026-01-01T00:00:10Z",
    )
    assert "pending_superseded" in [e.kind for e in events]
    assert state.superseded_drop_count == 1
    assert state.pending_request is not None
    assert state.pending_request.request_id != "11111111-1111-4111-8111-111111111111"


def _dummy_request() -> dict:
    return {
        "schema_version": 1,
        "request_id": "11111111-1111-4111-8111-111111111111",
        "attempt_id": "",
        "instance_id": "electrum-main",
        "created_at": "2026-01-01T00:00:00Z",
        "confirmed_at": "2026-01-01T00:00:00Z",
        "boot_id": "boot-1",
        "config_fingerprint": "x",
        "app": "electrum",
        "data_dir": "/home/user/.electrum",
        "matcher": {},
        "job_config": "/jobs/app.yaml",
        "job_params": {},
        "vaultkeep": "/usr/local/bin/vaultkeep",
        "environment": {},
        "preflight_version": None,
        "deadline_seconds": None,
    }


@pytest.mark.parametrize("utc", ["2027-01-01T00:00:00Z", "2025-01-01T00:00:00Z"])
def test_confirmation_uses_monotonic_elapsed_despite_wall_clock_jumps(utc: str) -> None:
    instance = make_instance(confirm_seconds=5)
    state = InstanceState(instance_id=instance.id)
    progress(state, instance, _detection(Observation.RUNNING), CONTEXT, monotonic_now=0)
    progress(state, instance, _detection(Observation.STOPPED), CONTEXT, monotonic_now=1)
    progress(
        state,
        instance,
        _detection(Observation.STOPPED),
        CONTEXT,
        monotonic_now=2,
        utc_now_value=utc,
    )
    assert state.pending_request is None
    progress(
        state,
        instance,
        _detection(Observation.STOPPED),
        CONTEXT,
        monotonic_now=6,
        utc_now_value=utc,
    )
    assert state.pending_request is not None


def test_persisted_candidate_needs_a_full_new_delay() -> None:
    instance = make_instance(confirm_seconds=5)
    saved = InstanceState(
        instance_id=instance.id,
        armed=True,
        boot_id="boot-1",
        config_fingerprint=instance.fingerprint(),
        candidate_since="2020-01-01T00:00:00Z",
    )
    state = InstanceState.from_dict(saved.to_dict())
    progress(state, instance, _detection(Observation.STOPPED), CONTEXT, monotonic_now=100)
    assert state.pending_request is None
    progress(state, instance, _detection(Observation.STOPPED), CONTEXT, monotonic_now=105)
    assert state.pending_request is not None


def test_recovered_pending_keeps_identity_and_reconfirms_after_uncertainty() -> None:
    instance = make_instance(confirm_seconds=5)
    request = replace(make_request(instance), attempt_id="", boot_id="boot-1")
    state = InstanceState.from_dict(
        InstanceState(
            instance_id=instance.id,
            pending_request=request,
            boot_id="boot-1",
            config_fingerprint=instance.fingerprint(),
        ).to_dict()
    )
    progress(state, instance, _detection(Observation.STOPPED), CONTEXT, monotonic_now=0)
    progress(state, instance, _detection(Observation.AMBIGUOUS), CONTEXT, monotonic_now=4)
    progress(state, instance, _detection(Observation.STOPPED), CONTEXT, monotonic_now=5)
    progress(state, instance, _detection(Observation.STOPPED), CONTEXT, monotonic_now=9)
    assert state.pending_reconfirmation_required
    progress(state, instance, _detection(Observation.STOPPED), CONTEXT, monotonic_now=10)
    assert not state.pending_reconfirmation_required
    assert (
        state.pending_request is not None and state.pending_request.request_id == request.request_id
    )


def test_changed_matcher_cannot_reuse_running_evidence() -> None:
    instance = make_instance()
    state = InstanceState(
        instance_id=instance.id,
        armed=True,
        candidate_monotonic=0,
        candidate_since="2020-01-01T00:00:00Z",
        config_fingerprint="old",
    )
    progress(state, instance, _detection(Observation.STOPPED), CONTEXT, monotonic_now=100)
    assert not state.armed and state.pending_request is None


def test_confirmed_pending_requires_new_delay_after_ambiguous_observation() -> None:
    instance = make_instance(confirm_seconds=5)
    request = replace(make_request(instance), attempt_id="", boot_id="boot-1")
    state = InstanceState(
        instance_id=instance.id,
        pending_request=request,
        config_fingerprint=instance.fingerprint(),
        boot_id="boot-1",
    )
    progress(state, instance, _detection(Observation.AMBIGUOUS), CONTEXT, monotonic_now=0)
    progress(state, instance, _detection(Observation.STOPPED), CONTEXT, monotonic_now=1)
    assert state.pending_reconfirmation_required
    progress(state, instance, _detection(Observation.STOPPED), CONTEXT, monotonic_now=6)
    assert not state.pending_reconfirmation_required
    assert (
        state.pending_request is not None and state.pending_request.request_id == request.request_id
    )
