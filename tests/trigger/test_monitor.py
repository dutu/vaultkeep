from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.trigger.conftest import (
    FakeController,
    FakeProvider,
    RecordingNotifier,
    make_config,
    make_instance,
    make_request,
)
from vaultkeep.trigger.errors import BlockedError
from vaultkeep.trigger.matcher import ProcessInfo
from vaultkeep.trigger.models import (
    AttemptOutcome,
    InstanceState,
    LaunchRecord,
    Observation,
    ObservationRecord,
    Receipt,
    RejectionState,
    utc_now,
)
from vaultkeep.trigger.monitor import Monitor, MonitorLock, unit_name_for
from vaultkeep.trigger.state import StateStore
from vaultkeep.trigger.systemd import SubmissionClass, SubmissionResult, UnitState

RUN_APP = "/opt/app/run_app"


def _running_proc() -> ProcessInfo:
    return ProcessInfo(
        pid=99,
        create_time=1.0,
        exe_realpath="/usr/bin/python3",
        cmdline=(RUN_APP, "--dir", "/home/user/.electrum"),
    )


def _monitor(store: StateStore, provider, controller, notifier, instance) -> Monitor:
    return Monitor(
        make_config(instance),
        store=store,
        provider=provider,
        controller=controller,
        notifier=notifier,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )


def _make(store, provider, controller=None, notifier=None, instance=None):
    controller = controller or FakeController()
    notifier = notifier or RecordingNotifier()
    instance = instance or make_instance(confirm_seconds=0.0)
    return _monitor(store, provider, controller, notifier, instance), controller, notifier


def test_full_lifecycle_dispatch_and_receipt(store: StateStore) -> None:
    provider = FakeProvider([_running_proc()])
    monitor, controller, _ = _make(store, provider)

    monitor.run_once()  # running -> armed
    initial = store.read_instance("electrum-main")
    assert initial is not None and initial.armed

    provider.processes = []
    monitor.run_once()  # stopped -> candidate
    monitor.run_once()  # stopped -> confirmed + dispatched

    state = store.read_instance("electrum-main")
    assert state is not None
    assert state.active_attempt_id is not None
    assert state.pending_request is None
    assert controller.started == [unit_name_for(state.active_attempt_id)]

    attempt_id = state.active_attempt_id
    request_id = state.active_request_id
    assert request_id is not None
    store.write_receipt(
        Receipt(
            attempt_id=attempt_id,
            request_id=request_id,
            instance_id="electrum-main",
            outcome=AttemptOutcome.CREATED,
            started_at=utc_now(),
            ended_at=utc_now(),
            duration_seconds=1.0,
            exit_code=0,
        )
    )
    controller.states[unit_name_for(attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor.run_once()
    reconciled = store.read_instance("electrum-main")
    assert reconciled is not None
    assert reconciled.active_attempt_id is None
    assert reconciled.attempt_summaries[-1]["outcome"] == "created"


def test_rejected_submission_requeues(store: StateStore) -> None:
    provider = FakeProvider([_running_proc()])
    controller = FakeController(
        submission=SubmissionResult(SubmissionClass.REJECTED, 5, "no such unit", True)
    )
    monitor, controller, _ = _make(store, provider, controller=controller)

    monitor.run_once()
    provider.processes = []
    monitor.run_once()
    monitor.run_once()

    state = store.read_instance("electrum-main")
    assert state is not None
    assert state.active_attempt_id is None
    assert state.pending_request is not None
    assert state.rejection is not None and state.rejection.consecutive == 1
    assert state.retry_not_before is not None
    assert len(controller.started) == 1

    # The delayed retry is not dispatched immediately.
    monitor.run_once()
    assert len(controller.started) == 1


def test_deferred_receipt_schedules_retry(store: StateStore) -> None:
    provider = FakeProvider([_running_proc()])
    monitor, controller, _ = _make(store, provider)
    monitor.run_once()
    provider.processes = []
    monitor.run_once()
    monitor.run_once()
    state = store.read_instance("electrum-main")
    assert state is not None
    attempt_id = state.active_attempt_id
    request_id = state.active_request_id
    assert attempt_id is not None
    assert request_id is not None

    store.write_receipt(
        Receipt(
            attempt_id=attempt_id,
            request_id=request_id,
            instance_id="electrum-main",
            outcome=AttemptOutcome.DEFERRED,
            started_at=utc_now(),
            ended_at=utc_now(),
            duration_seconds=1.0,
            exit_code=10,
        )
    )
    controller.states[unit_name_for(attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor.run_once()
    updated = store.read_instance("electrum-main")
    assert updated is not None
    assert updated.active_attempt_id is None
    assert updated.pending_request is not None
    assert updated.retry_not_before is not None


def test_outcome_unknown_when_worker_absent(store: StateStore) -> None:
    provider = FakeProvider([_running_proc()])
    controller = FakeController()
    monitor, controller, notifier = _make(store, provider, controller=controller)
    monitor.run_once()
    provider.processes = []
    monitor.run_once()
    monitor.run_once()
    active_state = store.read_instance("electrum-main")
    assert active_state is not None
    attempt_id = active_state.active_attempt_id
    assert attempt_id is not None

    # Worker is absent and the unit reports inactive: outcome unknown, no replay.
    controller.states[unit_name_for(attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor.run_once()
    state = store.read_instance("electrum-main")
    assert state is not None
    assert state.active_attempt_id is None
    assert state.pending_request is None
    assert state.review_required is True
    assert state.attempt_summaries[-1]["outcome"] == "outcome_unknown"


def test_overdue_notification_sent_once(store: StateStore) -> None:
    provider = FakeProvider([_running_proc()])
    instance = make_instance(confirm_seconds=0.0, deadline_seconds=0.0)
    monitor, _controller, notifier = _make(store, provider, instance=instance)
    monitor.run_once()
    provider.processes = []
    monitor.run_once()
    monitor.run_once()  # confirmed and dispatched
    monitor.run_once()  # overdue check fires for the active attempt
    monitor.run_once()
    overdue = [c for c in notifier.calls if "overdue" in c[0]]
    assert len(overdue) == 1


def test_inactive_unit_no_receipt_with_rejection_requeues(store: StateStore) -> None:
    provider = FakeProvider([_running_proc()])
    controller = FakeController(
        submission=SubmissionResult(SubmissionClass.INCONCLUSIVE, 1, "generic", True)
    )
    monitor, controller, _ = _make(store, provider, controller=controller)
    monitor.run_once()
    provider.processes = []
    monitor.run_once()
    monitor.run_once()
    state = store.read_instance("electrum-main")
    assert state is not None
    attempt_id = state.active_attempt_id
    assert attempt_id is not None
    # Persist proven rejection evidence on the launch marker.
    launch = store.read_launch(attempt_id)
    assert launch is not None
    store.write_launch(replace(launch, rejected_before_queue=True, rejection_code=5))
    controller.states[unit_name_for(attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor.run_once()
    updated = store.read_instance("electrum-main")
    assert updated is not None
    assert updated.active_attempt_id is None
    assert updated.pending_request is not None
    assert updated.retry_not_before is not None


def test_monitor_lock_is_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "monitor.lock"
    first = MonitorLock(path)
    first.acquire()
    try:
        second = MonitorLock(path)
        with pytest.raises(BlockedError):
            second.acquire()
    finally:
        first.release()
    # After release the lock can be acquired.
    third = MonitorLock(path)
    third.acquire()
    third.release()


def _saved_attempt(store: StateStore, *, instance=None, outcome=None, pending=None):
    instance = instance or make_instance()
    request = make_request(instance)
    store.write_request(request)
    store.write_launch(
        LaunchRecord(request.attempt_id, unit_name_for(request.attempt_id), utc_now())
    )
    state = InstanceState(
        instance_id=instance.id,
        config_fingerprint=instance.fingerprint(),
        active_attempt_id=request.attempt_id,
        active_request_id=request.request_id,
        latest_request_id=request.request_id,
        pending_request=pending,
        last_monitor_update="2020-01-01T00:00:00Z",
        last_observation=ObservationRecord(Observation.STOPPED, utc_now()),
    )
    store.write_instance(state)
    if outcome is not None:
        store.write_receipt(
            Receipt(
                request.attempt_id,
                request.request_id,
                instance.id,
                outcome,
                utc_now(),
                utc_now(),
                1,
                exit_code=10 if outcome is AttemptOutcome.DEFERRED else 0,
            )
        )
    return instance, request


@pytest.mark.parametrize("receipt_present", [False, True])
def test_queued_job_holds_active_slot_even_with_receipt(
    store: StateStore, receipt_present: bool
) -> None:
    instance, request = _saved_attempt(
        store, outcome=AttemptOutcome.CREATED if receipt_present else None
    )
    monitor, controller, notifier = _make(store, FakeProvider([]), instance=instance)
    unit = unit_name_for(request.attempt_id)
    controller.states[unit] = UnitState("loaded", "inactive", "dead", job_pending=True)
    monitor.run_once()
    monitor.run_once()
    state = store.read_instance(instance.id)
    assert state is not None and state.active_attempt_id == request.attempt_id
    assert not controller.started
    if receipt_present:
        assert len(state.attempt_summaries) == 1
        assert not [title for title, _body in notifier.calls if "finished" in title]
    controller.states[unit] = UnitState("loaded", "inactive", "dead")
    monitor.run_once()
    state = store.read_instance(instance.id)
    assert state is not None and state.active_attempt_id is None
    if receipt_present:
        assert len([title for title, _body in notifier.calls if "finished" in title]) == 1


def test_receipt_preserves_slot_while_manager_is_unavailable(store: StateStore) -> None:
    instance, request = _saved_attempt(store, outcome=AttemptOutcome.CREATED)
    monitor, _controller, _notifier = _make(store, FakeProvider([]), instance=instance)
    monitor.run_once()
    state = store.read_instance(instance.id)
    assert state is not None and state.active_attempt_id == request.attempt_id
    assert state.active_outcome_pending


def test_receipt_during_monitor_gap_is_annotated_before_success_notice(store: StateStore) -> None:
    instance, request = _saved_attempt(store, outcome=AttemptOutcome.CREATED)
    monitor, controller, notifier = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor._startup_reconcile()
    state = store.read_instance(instance.id)
    assert state is not None and state.review_required
    assert state.attempt_summaries[0]["review_required"]
    finished = [body for title, body in notifier.calls if "finished" in title]
    assert len(finished) == 1 and "review required" in finished[0]
    monitor._startup_reconcile()
    assert len([title for title, _body in notifier.calls if "finished" in title]) == 1


def test_newer_pending_survives_old_deferred_receipt(store: StateStore) -> None:
    instance, old = _saved_attempt(store, outcome=AttemptOutcome.DEFERRED)
    state = store.read_instance(instance.id)
    assert state is not None
    newer = replace(make_request(instance), attempt_id="")
    state.pending_request = newer
    state.latest_request_id = newer.request_id
    store.write_instance(state)
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(old.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor._startup_reconcile()
    recovered = store.read_instance(instance.id)
    assert recovered is not None and recovered.pending_request is not None
    assert recovered.pending_request.request_id == newer.request_id
    assert recovered.retry_not_before is None


def test_restart_invalidates_retry_of_old_active_request(store: StateStore) -> None:
    instance, old = _saved_attempt(store)
    provider = FakeProvider([_running_proc()])
    monitor, controller, _ = _make(store, provider, instance=instance)
    unit = unit_name_for(old.attempt_id)
    controller.states[unit] = UnitState("loaded", "activating", "start")
    monitor.run_once()  # restart invalidates the old shutdown
    provider.processes = []
    store.write_receipt(
        Receipt(
            old.attempt_id,
            old.request_id,
            instance.id,
            AttemptOutcome.DEFERRED,
            utc_now(),
            utc_now(),
            1,
            exit_code=10,
        )
    )
    controller.states[unit] = UnitState("loaded", "inactive", "dead")
    monitor.run_once()
    state = store.read_instance(instance.id)
    assert state is not None and state.pending_request is None


def test_recovered_pending_does_not_dispatch_until_full_confirmation(store: StateStore) -> None:
    instance = make_instance(confirm_seconds=5)
    pending = replace(make_request(instance), attempt_id="")
    store.write_instance(
        InstanceState(
            instance_id=instance.id,
            pending_request=pending,
            config_fingerprint=instance.fingerprint(),
        )
    )
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    now = [0.0]
    monitor._clock = lambda: now[0]
    monitor._startup_reconcile()
    monitor.run_once()
    now[0] = 4
    monitor.run_once()
    assert not controller.started
    now[0] = 5
    monitor.run_once()
    assert len(controller.started) == 1
    dispatched = store.read_instance(instance.id)
    assert dispatched is not None and dispatched.active_attempt_id is not None
    request = store.read_request(dispatched.active_attempt_id)
    assert request is not None and request.request_id == pending.request_id


def test_repeated_proven_rejections_remain_retryable_with_bounded_history(
    store: StateStore,
) -> None:
    instance = make_instance(confirm_seconds=0)
    controller = FakeController(
        submission=SubmissionResult(SubmissionClass.REJECTED, 5, "no unit", True)
    )
    monitor, controller, notifier = _make(
        store, FakeProvider([]), controller=controller, instance=instance
    )
    state = InstanceState(
        instance_id=instance.id,
        pending_request=replace(make_request(instance), attempt_id=""),
        last_observation=ObservationRecord(Observation.STOPPED, utc_now()),
    )
    for _ in range(80):
        state.retry_not_before = None
        monitor._dispatch(state, instance)
        monitor._cleanup(state)
    assert len(controller.started) == 80
    assert len(store.list_attempt_ids()) <= 20
    assert state.rejection is not None and state.rejection.consecutive == 80
    assert len(state.rejection.processed_attempts) == 80
    assert len([title for title, _body in notifier.calls if "requested" in title]) == 1
    assert len([title for title, _body in notifier.calls if "repeated submission" in title]) == 1


def test_rejection_recovered_after_crash_is_counted_exactly_once(store: StateStore) -> None:
    instance, request = _saved_attempt(store)
    launch = store.read_launch(request.attempt_id)
    assert launch is not None
    store.write_launch(replace(launch, rejected_before_queue=True, rejection_code=5))
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor._startup_reconcile()
    state = store.read_instance(instance.id)
    assert state is not None and state.rejection is not None and state.rejection.consecutive == 1
    monitor._startup_reconcile()
    state = store.read_instance(instance.id)
    assert state is not None and state.rejection is not None and state.rejection.consecutive == 1
    assert len(state.attempt_summaries) == 1


@pytest.mark.parametrize("removed", [False, True])
def test_disabled_or_removed_instances_continue_reconciliation(
    store: StateStore, removed: bool
) -> None:
    instance, request = _saved_attempt(store, outcome=AttemptOutcome.CREATED)
    controller = FakeController()
    unit = unit_name_for(request.attempt_id)
    controller.states[unit] = UnitState("loaded", "activating", "start")
    config = make_config() if removed else make_config(replace(instance, enabled=False))
    monitor = Monitor(
        config,
        store=store,
        provider=FakeProvider([]),
        controller=controller,
        notifier=RecordingNotifier(),
    )
    monitor._startup_reconcile()
    active = store.read_instance(instance.id)
    assert active is not None and active.active_attempt_id == request.attempt_id
    controller.states[unit] = UnitState("loaded", "inactive", "dead")
    monitor.run_once()
    state = store.read_instance(instance.id)
    assert state is not None and state.active_attempt_id is None and state.review_required
    assert len(state.attempt_summaries) == 1 and not controller.started


def test_wait_notification_uses_frozen_deadline_and_survives_later_waits(store: StateStore) -> None:
    instance, request = _saved_attempt(store, instance=make_instance(deadline_seconds=10))
    monitor, _controller, notifier = _make(
        store, FakeProvider([]), instance=make_instance(deadline_seconds=None)
    )
    state = monitor._load_state(instance.id)
    state.pending_request = replace(make_request(instance), attempt_id="")
    state.blocking_attempt_id = request.attempt_id
    state.blocked_wait_started_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    monitor._update_blocked_wait(state)
    assert not notifier.calls
    # A separate attempt with no frozen deadline uses the default wait alert.
    other = make_request(make_instance(deadline_seconds=None))
    store.write_request(other)
    state.active_attempt_id = other.attempt_id
    state.blocking_attempt_id = other.attempt_id
    monitor._update_blocked_wait(state)
    assert len(notifier.calls) == 1
    state.pending_request = None
    monitor._update_blocked_wait(state)
    state.pending_request = replace(make_request(instance), attempt_id="")
    monitor._update_blocked_wait(state)
    state.blocked_wait_started_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    monitor._update_blocked_wait(state)
    assert len(notifier.calls) == 1
    monitor._persist(state)
    restored = store.read_instance(instance.id)
    assert restored is not None
    monitor._update_blocked_wait(restored)
    assert len(notifier.calls) == 1


def test_delayed_poll_marks_execution_for_review_before_applying_receipt(store: StateStore) -> None:
    instance, request = _saved_attempt(store, outcome=AttemptOutcome.CREATED)
    monitor, controller, notifier = _make(store, FakeProvider([]), instance=instance)
    monitor._last_poll[instance.id] = 0
    monitor._clock = lambda: 10
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor.run_once()
    state = store.read_instance(instance.id)
    assert (
        state is not None
        and state.review_required
        and state.attempt_summaries[0]["review_required"]
    )
    assert any("review required" in body for title, body in notifier.calls if "finished" in title)


def test_positive_worker_evidence_resets_rejection_count_without_rearming_alerts(
    store: StateStore,
) -> None:
    instance, request = _saved_attempt(store)
    state = store.read_instance(instance.id)
    assert state is not None
    state.rejection = RejectionState(
        request_id=request.request_id,
        consecutive=5,
        start_notified=True,
        escalation_attempted=True,
        reminders_suppressed=True,
    )
    store.write_instance(state)
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState(
        "loaded", "activating", "start"
    )
    monitor.run_once()
    restored = store.read_instance(instance.id)
    assert restored is not None and restored.rejection is not None
    assert restored.rejection.consecutive == 0
    assert restored.rejection.start_notified and restored.rejection.escalation_attempted
    assert restored.rejection.reminders_suppressed
