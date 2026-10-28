from __future__ import annotations

import json
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
from vaultkeep.application import Observation, ProcessInfo
from vaultkeep.notifications.notifier import NotificationConfig
from vaultkeep.trigger.activity import application_status
from vaultkeep.trigger.errors import BlockedError
from vaultkeep.trigger.models import (
    AttemptOutcome,
    InstanceState,
    LaunchRecord,
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


@pytest.mark.parametrize("outcome", [AttemptOutcome.CREATED, AttemptOutcome.UNCHANGED])
def test_full_lifecycle_dispatch_receipt_and_restart(store: StateStore, outcome) -> None:
    provider = FakeProvider([_running_proc()])
    monitor, controller, notifier = _make(store, provider)

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
            outcome=outcome,
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
    assert reconciled.attempt_summaries[-1]["outcome"] == outcome.value
    assert (
        application_status(
            reconciled,
            store,
            reliable=True,
            poll_seconds=monitor.config.poll_seconds,
            monitor_started_at=monitor._started_at,
        )[0]
        == "HAS STOPPED"
    )
    requested = [(title, body) for title, body in notifier.calls if "requested" in title]
    assert len(requested) == 1
    assert "dispatch requested" in requested[0][1]
    assert "starting" not in requested[0][1]
    assert not any(
        "Backup completed" in title or "backup finished" in title for title, _ in notifier.calls
    )

    restarted, _, restarted_notifier = _make(store, provider, controller=controller)
    restarted._startup_reconcile()
    assert (
        application_status(
            store.read_instance(reconciled.instance_id),
            store,
            reliable=True,
            poll_seconds=restarted.config.poll_seconds,
            monitor_started_at=restarted._started_at,
        )[0]
        == "UNKNOWN"
    )
    restarted.run_once()
    refreshed = store.read_instance(reconciled.instance_id)
    assert application_status(
        refreshed,
        store,
        reliable=True,
        poll_seconds=restarted.config.poll_seconds,
        monitor_started_at=restarted._started_at,
    ) == ("NOT RUNNING", "")
    assert not refreshed.armed and refreshed.pending_request is None
    assert refreshed.latest_request_id == request_id
    assert refreshed.attempt_summaries == reconciled.attempt_summaries
    assert store.read_request(attempt_id) is not None
    assert controller.started == [unit_name_for(attempt_id)]
    assert not restarted_notifier.calls


@pytest.mark.parametrize("enabled", [False, True])
def test_notification_failure_does_not_change_dispatch_or_receipt_recovery(
    store, monkeypatch, enabled
):
    provider = FakeProvider([_running_proc()])
    monitor, controller, notifier = _make(store, provider)
    monitor.config = replace(monitor.config, notifications=NotificationConfig(enabled=enabled))

    def fail(*_args, **_kwargs):
        raise OSError("desktop unavailable")

    monkeypatch.setattr(notifier, "notify", fail)
    monitor.run_once()
    provider.processes = []
    monitor.run_once()
    monitor.run_once()
    state = store.read_instance("electrum-main")
    assert state.active_attempt_id is not None
    assert controller.started == [unit_name_for(state.active_attempt_id)]
    store.write_receipt(
        Receipt(
            state.active_attempt_id,
            state.active_request_id,
            state.instance_id,
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
    monitor.run_once()
    reconciled = store.read_instance("electrum-main")
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
    assert state.attempt_summaries[-1]["outcome"] == "outcome_unknown"
    assert notifier.calls[-1][0] == "VaultKeep Trigger: Outcome unknown"
    assert notifier.options[-1] == ("trigger:electrum-main", True)
    count = len(notifier.calls)
    monitor.run_once()
    assert len(notifier.calls) == count


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


def test_application_notifications_follow_status_states_without_repeating_polls(store):
    provider = FakeProvider([])
    monitor, _controller, notifier = _make(store, provider)
    monitor.config = replace(monitor.config, notifications=NotificationConfig(enabled=True))
    monitor.run_once()  # First observation establishes NOT RUNNING.
    monitor.run_once()
    assert not notifier.calls
    provider.processes = [_running_proc()]
    monitor.run_once()
    monitor.run_once()
    provider.processes = []
    monitor.run_once()
    monitor.run_once()  # Confirms shutdown and dispatches.
    monitor.run_once()
    provider.processes = [_running_proc()]
    monitor.run_once()
    changes = [
        (body, options)
        for (title, body), options in zip(notifier.calls, notifier.options, strict=True)
        if "Application state changed" in title
    ]
    assert changes == [
        (f"instance electrum-main: Application {before} → {after}", (None, False))
        for before, after in [
            ("NOT RUNNING", "RUNNING"),
            ("RUNNING", "CONFIRMING STOP"),
            ("CONFIRMING STOP", "HAS STOPPED"),
            ("HAS STOPPED", "RUNNING"),
        ]
    ]


@pytest.mark.parametrize("uncertain", [Observation.AMBIGUOUS, Observation.INSPECTION_UNAVAILABLE])
def test_application_notification_reports_unknown_and_recovery(store, monkeypatch, uncertain):
    from vaultkeep.application import DetectionResult

    provider = FakeProvider([_running_proc()])
    monitor, controller, notifier = _make(store, provider)
    monitor.config = replace(monitor.config, notifications=NotificationConfig(enabled=True))
    monitor.run_once()
    with monkeypatch.context() as context:
        context.setattr(
            "vaultkeep.trigger.monitor.detect", lambda *_args: DetectionResult(uncertain)
        )
        monitor.run_once()
        monitor.run_once()
    monitor.run_once()
    assert [body for _title, body in notifier.calls] == [
        "instance electrum-main: Application RUNNING → UNKNOWN",
        "instance electrum-main: Application UNKNOWN → RUNNING",
    ]
    restarted = Monitor(
        monitor.config, store=store, provider=provider, controller=controller, notifier=notifier
    )
    restarted.run_once()
    assert len(notifier.calls) == 2


@pytest.mark.parametrize(
    "outcome",
    [
        AttemptOutcome.CREATED,
        AttemptOutcome.UNCHANGED,
        AttemptOutcome.BACKUP_FAILED,
    ],
)
@pytest.mark.parametrize("historical_review", [False, True])
def test_receipts_reconcile_without_backup_result_notifications(store, outcome, historical_review):
    instance, request = _saved_attempt(store, outcome=outcome)
    if historical_review:
        state = store.read_instance(instance.id)
        state.attempt_summaries.append(
            {
                "attempt_id": "earlier-attempt",
                "request_id": "earlier-request",
                "outcome": "created",
            }
        )
        store.write_instance(state)
    monitor, controller, notifier = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "active", "running")
    monitor.run_once()
    assert not notifier.calls
    assert store.read_instance(instance.id).active_outcome_pending
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor.run_once()
    monitor.run_once()
    assert not notifier.calls
    state = store.read_instance(instance.id)
    assert state.active_attempt_id is None


@pytest.mark.parametrize("enabled", [False, True])
def test_trigger_never_reads_backup_status_for_notifications(store, monkeypatch, enabled):
    instance, request = _saved_attempt(store)
    monitor, controller, notifier = _make(store, FakeProvider([]), instance=instance)
    monitor.config = replace(monitor.config, notifications=NotificationConfig(enabled=enabled))
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "active", "running")

    def fail(*_args, **_kwargs):
        pytest.fail("Trigger must not read backup status for notifications")

    monkeypatch.setattr("vaultkeep.backup.status.backup_status", fail)
    monitor.run_once()
    monitor.run_once()
    assert not notifier.calls


@pytest.mark.parametrize(
    "outcome, title",
    [
        (AttemptOutcome.BLOCKED, "Dispatch blocked"),
        (AttemptOutcome.INTEGRATION_ERROR, "Dispatch failed"),
        (AttemptOutcome.RESULT_UNKNOWN, "Dispatch result unknown"),
    ],
)
def test_dispatch_outcome_notifications_wait_for_worker_and_deduplicate(store, outcome, title):
    instance, request = _saved_attempt(store, outcome=outcome)
    monitor, controller, notifier = _make(store, FakeProvider([]), instance=instance)
    unit = unit_name_for(request.attempt_id)
    controller.states[unit] = UnitState("loaded", "inactive", "dead", job_pending=True)
    monitor.run_once()
    assert not notifier.calls
    controller.states[unit] = UnitState("loaded", "inactive", "dead")
    monitor.run_once()
    monitor.run_once()
    assert len(notifier.calls) == 1
    assert notifier.calls[0][0] == f"VaultKeep Trigger: {title}"
    assert notifier.options == [(f"trigger:{instance.id}", True)]


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
        assert not [title for title, _body in notifier.calls if "finished" in title]


def test_receipt_preserves_slot_while_manager_is_unavailable(store: StateStore) -> None:
    instance, request = _saved_attempt(store, outcome=AttemptOutcome.CREATED)
    monitor, _controller, _notifier = _make(store, FakeProvider([]), instance=instance)
    monitor.run_once()
    state = store.read_instance(instance.id)
    assert state is not None and state.active_attempt_id == request.attempt_id
    assert state.active_outcome_pending


def test_receipt_during_monitor_gap_is_annotated_before_recovery_notice(store: StateStore) -> None:
    instance, request = _saved_attempt(store, outcome=AttemptOutcome.CREATED)
    monitor, controller, notifier = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor._startup_reconcile()
    state = store.read_instance(instance.id)
    assert state.active_attempt_id is None
    assert state.attempt_summaries[-1]["outcome"] == "created"
    review = [body for title, body in notifier.calls if "Review required" in title]
    assert review == []
    monitor._startup_reconcile()
    assert not [title for title, _body in notifier.calls if "finished" in title]


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
    assert len([title for title, _body in notifier.calls if "Repeated submission" in title]) == 1


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
    assert state.active_attempt_id is None
    assert not any("Review required" in title for title, _body in notifier.calls)


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


def _review_status(store, instance_id, capsys, *, verbose=False):
    from unittest.mock import patch

    from vaultkeep.trigger.status import trigger_status

    instance = make_instance(instance_id=instance_id)
    monitor, _, _ = _make(store, FakeProvider([]), instance=instance)
    config = replace(
        monitor.config, job_id=instance_id, scope="a" * 16, log_level="DEBUG" if verbose else "INFO"
    )
    with patch("vaultkeep.trigger.status.state_root", return_value=store.root):
        result = trigger_status(config)
    assert not result.get("read_error"), result
    assert "safety" not in result
    return json.dumps([])


@pytest.mark.parametrize("recovery", ["startup", "delayed_poll", "retired"])
@pytest.mark.parametrize("evidence", ["blocked", "already_observed", "receipt_reconciled"])
def test_gap_does_not_create_review_when_execution_was_excluded(store, recovery, evidence, capsys):
    outcome = AttemptOutcome.BLOCKED if evidence == "blocked" else AttemptOutcome.CREATED
    instance, request = _saved_attempt(store)
    store.write_receipt(
        Receipt(
            request.attempt_id,
            request.request_id,
            instance.id,
            outcome,
            "2020-01-01T00:00:00Z",
            "2020-01-01T00:01:00Z",
            60,
        )
    )
    receipt_bytes = store.receipt_path(request.attempt_id).read_bytes()
    state = store.read_instance(instance.id)
    if evidence == "already_observed":
        state.last_observation = ObservationRecord(Observation.STOPPED, "2020-01-01T00:02:00Z")
    elif evidence == "receipt_reconciled":
        state.active_outcome_pending = True
        state.last_observation = None
    store.write_instance(state)
    if recovery == "retired":
        instance = replace(instance, enabled=False)
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    if recovery == "startup":
        monitor._startup_reconcile()
    else:
        monitor._last_poll[instance.id] = 0
        monitor._clock = lambda: 10
        monitor.run_once()
    updated = store.read_instance(instance.id)
    assert updated.active_attempt_id is None
    assert _review_status(store, instance.id, capsys) == "[]"
    assert store.receipt_path(request.attempt_id).read_bytes() == receipt_bytes


def test_recent_state_write_does_not_cover_an_observation_gap(store):
    instance, request = _saved_attempt(store, outcome=AttemptOutcome.CREATED)
    state = store.read_instance(instance.id)
    state.last_monitor_update = "2999-01-01T00:00:00Z"
    store.write_instance(state)
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor._startup_reconcile()
    updated = store.read_instance(instance.id)
    assert updated.active_attempt_id is None


def test_receipt_proves_execution_despite_a_saved_submission_rejection(store):
    instance, request = _saved_attempt(store, outcome=AttemptOutcome.CREATED)
    launch = store.read_launch(request.attempt_id)
    store.write_launch(replace(launch, rejected_before_queue=True))
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor._startup_reconcile()


@pytest.mark.parametrize(
    "outcome", [AttemptOutcome.CREATED, AttemptOutcome.UNCHANGED, AttemptOutcome.RESULT_UNKNOWN]
)
@pytest.mark.parametrize(
    "previous_outcome", [AttemptOutcome.CREATED, AttemptOutcome.BACKUP_FAILED, None]
)
def test_later_success_preserves_real_uncertainty_across_restart(
    store, capsys, outcome, previous_outcome
):
    instance, previous = _saved_attempt(store, outcome=previous_outcome)
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(previous.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor._startup_reconcile()
    previous_summary = dict(store.read_instance(instance.id).attempt_summaries[-1])
    current = make_request(instance)
    store.write_request(current)
    store.write_launch(
        LaunchRecord(current.attempt_id, unit_name_for(current.attempt_id), utc_now())
    )
    store.write_receipt(
        Receipt(
            current.attempt_id,
            current.request_id,
            instance.id,
            outcome,
            utc_now(),
            utc_now(),
            1,
            exit_code=0,
        )
    )
    state = store.read_instance(instance.id)
    state.active_attempt_id = current.attempt_id
    state.active_request_id = current.request_id
    state.last_observation = ObservationRecord(Observation.STOPPED, utc_now())
    store.write_instance(state)
    controller.states[unit_name_for(current.attempt_id)] = UnitState("loaded", "inactive", "dead")
    for _ in range(2):
        restarted, _, _ = _make(store, FakeProvider([]), controller=controller, instance=instance)
        restarted._startup_reconcile()
    updated = store.read_instance(instance.id)
    assert updated.attempt_summaries[0] == previous_summary
    output = _review_status(store, instance.id, capsys, verbose=True)
    assert output == "[]"


def test_delay_and_restart_preserve_risk_while_worker_is_still_running(store):
    instance, request = _saved_attempt(store)
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState(
        "loaded", "activating", "start"
    )
    monitor._last_poll[instance.id] = 0
    monitor._clock = lambda: 10
    monitor.run_once()
    restarted, _, _ = _make(store, FakeProvider([]), controller=controller, instance=instance)
    restarted._startup_reconcile()
    restarted.run_once()
    assert store.read_instance(instance.id).active_attempt_id == request.attempt_id


def test_contradictory_blocked_receipt_cannot_clear_review(store):
    instance, request = _saved_attempt(store)
    state = store.read_instance(instance.id)
    store.write_instance(state)
    store.write_receipt(
        Receipt(
            request.attempt_id,
            "wrong-request",
            instance.id,
            AttemptOutcome.BLOCKED,
            utc_now(),
            utc_now(),
            0,
        )
    )
    monitor, _, _ = _make(store, FakeProvider([]), instance=instance)
    monitor._startup_reconcile()
    assert store.read_blocked(instance.id) is not None


@pytest.mark.parametrize(
    ("started", "ended", "observed"),
    [
        ("2020-01-01T00:00:00Z", "2020-01-01T00:01:00Z", "2999-01-01T00:00:00Z"),
        ("2020-01-01T00:02:00Z", "2020-01-01T00:01:00Z", "2020-01-01T00:03:00Z"),
        ("unavailable", "2020-01-01T00:01:00Z", "2020-01-01T00:03:00Z"),
        ("2020-01-01T00:00:00Z", "unavailable", "2020-01-01T00:03:00Z"),
    ],
)
def test_unreliable_timestamps_cannot_exclude_execution_gap(store, started, ended, observed):
    instance, request = _saved_attempt(store)
    store.write_receipt(
        Receipt(
            request.attempt_id,
            request.request_id,
            instance.id,
            AttemptOutcome.CREATED,
            started,
            ended,
            60,
        )
    )
    state = store.read_instance(instance.id)
    state.last_observation = ObservationRecord(Observation.STOPPED, observed)
    store.write_instance(state)
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor._startup_reconcile()


def test_completed_blocked_summary_cannot_contradict_receipt_and_clear_warning(store):
    instance, request = _saved_attempt(store, outcome=AttemptOutcome.BLOCKED)
    state = store.read_instance(instance.id)
    state.attempt_summaries.append(
        {
            "attempt_id": request.attempt_id,
            "request_id": request.request_id,
            "outcome": "created",
        }
    )
    store.write_instance(state)
    monitor, _, _ = _make(store, FakeProvider([]), instance=instance)
    monitor._startup_reconcile()
    assert store.read_blocked(instance.id) is not None


def test_history_cleanup_keeps_unresolved_attempt_identifiable(store, capsys):
    instance = make_instance()
    monitor, _, _ = _make(store, FakeProvider([]), instance=instance)
    state = InstanceState(instance_id=instance.id)
    for _index in range(51):
        request = make_request(instance)
        store.write_request(request)
        state.attempt_summaries.append(
            {
                "attempt_id": request.attempt_id,
                "request_id": request.request_id,
                "outcome": "created",
            }
        )
    oldest = state.attempt_summaries[0]["attempt_id"]
    store.write_instance(state)
    monitor._cleanup(state)
    assert not store.request_path(oldest).exists()
    updated = store.read_instance(instance.id)
    assert updated.attempt_summaries
    output = _review_status(store, instance.id, capsys, verbose=True)
    assert output == "[]"
