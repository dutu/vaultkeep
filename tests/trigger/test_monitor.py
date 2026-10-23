from __future__ import annotations

import json
import time
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
from vaultkeep.backup.execution import Execution
from vaultkeep.backup.paths import WorkflowPaths
from vaultkeep.state.identity import job_identity_hash
from vaultkeep.trigger import monitor as monitor_module
from vaultkeep.trigger.config import NotificationConfig
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
    assert notifier.options == [("electrum-main", False), ("electrum-main", True)]


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


@pytest.fixture
def live_activity(store, monkeypatch):
    instance, request = _saved_attempt(store)
    provider = FakeProvider([])
    monkeypatch.setattr(provider, "alive", lambda _pid, _created: True)
    monitor, controller, notifier = _make(store, provider, instance=instance)
    monitor.config = replace(monitor.config, notifications=NotificationConfig(enabled=True))
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "active", "running")
    paths = WorkflowPaths(store.root / "backup", store.root / "cache", store.root / "locks")
    identity = job_identity_hash(
        Path(request.job_config), request.instance_id, runtime_params=request.job_params
    )
    monkeypatch.setattr("vaultkeep.trigger.monitor.user_workflow_paths", lambda **_kwargs: paths)
    monkeypatch.setattr(
        "vaultkeep.diagnostics.utc_now", lambda: monitor_module._now_dt().isoformat()
    )
    with Execution(paths, request.instance_id, identity) as execution:
        reporter = execution._reporter
        reporter._path.unlink()  # No live phase has been observed by the monitor yet.
        yield monitor, controller, notifier, request, reporter
        execution.complete("created")


def test_phase_notifications_deduplicate_polls_and_finish_after_worker_ends(live_activity, store):
    monitor, controller, notifier, request, reporter = live_activity
    reporter.report("Creating and compressing archive")
    monitor.run_once()
    monitor.run_once()
    reporter.heartbeat()
    monitor.run_once()
    assert notifier.calls == [
        (
            "VaultKeep Trigger: backup running",
            "instance electrum-main: Creating and compressing archive",
        )
    ]
    reporter.report("Verifying archive")
    monitor.run_once()
    assert notifier.calls[-1][1] == "instance electrum-main: Verifying archive"
    assert len(notifier.calls) == 2

    # Finishing telemetry does not authorize a successful result notification.
    reporter.finish()
    monitor.run_once()
    assert notifier.calls[-1] == (
        "VaultKeep Trigger: backup finishing",
        "instance electrum-main: Finishing backup; awaiting result",
    )
    store.write_receipt(
        Receipt(
            request.attempt_id,
            request.request_id,
            request.instance_id,
            AttemptOutcome.CREATED,
            utc_now(),
            utc_now(),
            1,
            exit_code=0,
        )
    )
    monitor.run_once()
    assert len(notifier.calls) == 3
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor.run_once()
    monitor.run_once()
    assert (notifier.calls[-1][0], notifier.calls[-1][1].splitlines()[0]) == (
        "VaultKeep Trigger: backup finished",
        "instance electrum-main: new archive created",
    )
    assert notifier.options == [(request.instance_id, False)] * 3 + [(request.instance_id, True)]
    assert not monitor._last_activity


def test_phase_notification_refreshes_every_ten_seconds_and_resets_on_change(
    live_activity, monkeypatch
):
    monitor, _controller, notifier, _request, reporter = live_activity
    start = datetime.now(UTC)
    seconds = [0.0]
    monitor._clock = lambda: seconds[0]
    monkeypatch.setattr(
        "vaultkeep.trigger.monitor._now_dt", lambda: start + timedelta(seconds=seconds[0])
    )
    monkeypatch.setattr(
        "vaultkeep.backup.progress._now",
        lambda: (start + timedelta(seconds=seconds[0])).isoformat(),
    )
    reporter.report("Checking existing backups")
    for elapsed in (0, 9, 10, 19, 20):
        seconds[0] = elapsed
        reporter.heartbeat()
        monitor.run_once()
    assert [body for _title, body in notifier.calls] == [
        "instance electrum-main: Checking existing backups",
        "instance electrum-main: Checking existing backups (10s)",
        "instance electrum-main: Checking existing backups (20s)",
    ]
    seconds[0] = 25
    reporter.report("Verifying archive")
    monitor.run_once()
    assert notifier.calls[-1][1] == "instance electrum-main: Verifying archive"
    seconds[0] = 34
    reporter.heartbeat()
    monitor.run_once()
    assert len(notifier.calls) == 4
    seconds[0] = 35
    reporter.heartbeat()
    monitor.run_once()
    assert notifier.calls[-1][1] == "instance electrum-main: Verifying archive (10s)"


def test_first_phase_notification_omits_elapsed_but_refresh_preserves_actual_phase_age(
    live_activity, monkeypatch
):
    monitor, _controller, notifier, _request, reporter = live_activity
    start = datetime.now(UTC)
    monkeypatch.setattr("vaultkeep.backup.progress._now", lambda: start.isoformat())
    reporter.report("Checking existing backups")
    monkeypatch.setattr("vaultkeep.trigger.monitor._now_dt", lambda: start + timedelta(seconds=20))
    monitor.run_once()
    assert notifier.calls[-1][1] == "instance electrum-main: Checking existing backups"
    monitor._clock = lambda: 10.0
    monkeypatch.setattr("vaultkeep.trigger.monitor._now_dt", lambda: start + timedelta(seconds=30))
    monkeypatch.setattr(
        "vaultkeep.backup.progress._now", lambda: (start + timedelta(seconds=30)).isoformat()
    )
    reporter.heartbeat()
    monitor.run_once()
    assert notifier.calls[-1][1] == "instance electrum-main: Checking existing backups (30s)"


@pytest.mark.parametrize("lost_progress", [False, True])
def test_waiting_notifications_refresh_and_stop_after_completion(
    live_activity, store, lost_progress
):
    monitor, controller, notifier, request, reporter = live_activity
    seconds = [0.0]
    monitor._clock = lambda: seconds[0]
    reporter.report("Verifying archive")
    monitor.run_once()
    if lost_progress:
        reporter._path.unlink()
    else:
        reporter.finish()
    monitor.run_once()
    for elapsed in range(1, 11):
        seconds[0] = elapsed
        monitor.run_once()
    phase = (
        "Backup progress unavailable; awaiting result"
        if lost_progress
        else "Finishing backup; awaiting result"
    )
    assert notifier.calls[-1][1] == f"instance electrum-main: {phase} (10s)"
    store.write_receipt(
        Receipt(
            request.attempt_id,
            request.request_id,
            request.instance_id,
            AttemptOutcome.CREATED,
            utc_now(),
            utc_now(),
            1,
            exit_code=0,
        )
    )
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor.run_once()
    count = len(notifier.calls)
    for elapsed in range(11, 21):
        seconds[0] = elapsed
        monitor.run_once()
    assert len(notifier.calls) == count
    assert notifier.calls[-1][0] == "VaultKeep Trigger: backup finished"
    assert not monitor._last_activity


@pytest.mark.parametrize(
    "outcome",
    [
        AttemptOutcome.CREATED,
        AttemptOutcome.UNCHANGED,
        AttemptOutcome.BACKUP_FAILED,
        AttemptOutcome.BLOCKED,
        AttemptOutcome.RESULT_UNKNOWN,
        AttemptOutcome.INTEGRATION_ERROR,
    ],
)
def test_terminal_notification_has_actual_completion_time_in_local_timezone(
    live_activity, monkeypatch, outcome
):
    monitor, _controller, notifier, request, _reporter = live_activity
    # Use a deterministic non-UTC local timezone and restore the process timezone afterward.
    with monkeypatch.context() as context:
        context.setenv("TZ", "EET-2EEST,M3.5.0/3,M10.5.0/4")
        time.tzset()
        try:
            monitor._notify_terminal(
                monitor._load_state(request.instance_id),
                Receipt(
                    request.attempt_id,
                    request.request_id,
                    request.instance_id,
                    outcome,
                    "2026-10-22T09:00:00Z",
                    "2026-10-22T10:00:00Z",
                    3600,
                    exit_code=0,
                ),
            )
            assert notifier.calls[-1][1].endswith("\n2026-10-22 13:00:00 EEST (local time)")
            assert notifier.options[-1] == (request.instance_id, True)
        finally:
            context.undo()
            time.tzset()


def test_application_notifications_follow_status_states_without_repeating_polls(store):
    provider = FakeProvider([])
    monitor, _controller, notifier = _make(store, provider)
    monitor.config = replace(monitor.config, notifications=NotificationConfig(enabled=True))
    monitor.run_once()  # First observation establishes NOT YET DETECTED.
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
        if "application state changed" in title
    ]
    assert changes == [
        (f"instance electrum-main: Application {before} → {after}", (None, False))
        for before, after in [
            ("NOT YET DETECTED", "RUNNING"),
            ("RUNNING", "CONFIRMING STOP"),
            ("CONFIRMING STOP", "HAS STOPPED"),
            ("HAS STOPPED", "RUNNING"),
        ]
    ]


@pytest.mark.parametrize("uncertain", [Observation.AMBIGUOUS, Observation.INSPECTION_UNAVAILABLE])
def test_application_notification_reports_unknown_and_recovery(store, monkeypatch, uncertain):
    from vaultkeep.trigger.detection import DetectionResult

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
    ("outcome", "body"),
    [
        (AttemptOutcome.CREATED, "new archive created"),
        (AttemptOutcome.UNCHANGED, "no change; no new archive"),
        (AttemptOutcome.BACKUP_FAILED, "failed (exit 1, signal None)"),
    ],
)
@pytest.mark.parametrize("historical_review", [False, True])
def test_receipt_takes_precedence_over_phase_notifications(
    live_activity, store, outcome, body, historical_review
):
    monitor, controller, notifier, request, reporter = live_activity
    if historical_review:
        state = store.read_instance(request.instance_id)
        state.review_required = True
        state.review_reason = (
            "attempt earlier-attempt: monitoring gap overlapped possible execution"
        )
        state.attempt_summaries.append(
            {
                "attempt_id": "earlier-attempt",
                "request_id": "earlier-request",
                "outcome": "created",
                "review_required": True,
            }
        )
        store.write_instance(state)
    reporter.report("Verifying archive")
    store.write_receipt(
        Receipt(
            request.attempt_id,
            request.request_id,
            request.instance_id,
            outcome,
            utc_now(),
            utc_now(),
            1,
            exit_code=1 if outcome is AttemptOutcome.BACKUP_FAILED else 0,
        )
    )
    monitor.run_once()
    assert notifier.calls == [
        (
            "VaultKeep Trigger: backup finishing",
            "instance electrum-main: Finishing backup; awaiting result",
        )
    ]
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor.run_once()
    assert (notifier.calls[-1][0], notifier.calls[-1][1].splitlines()[0]) == (
        "VaultKeep Trigger: backup finished",
        f"instance electrum-main: {body}",
    )
    assert notifier.options[-1] == (request.instance_id, True)
    state = store.read_instance(request.instance_id)
    assert state.active_attempt_id is None
    assert not state.attempt_summaries[-1]["review_required"]
    assert state.review_required is historical_review
    if historical_review:
        assert state.attempt_summaries[0]["review_required"]
        assert state.review_reason.startswith("attempt earlier-attempt:")


@pytest.mark.parametrize(
    "change",
    [
        {"execution_id": "another-execution"},
        {"started_at": "invalid"},
        {"phase_started_at": "2000-01-01T00:00:00Z"},
        {"updated_at": "2000-01-01T00:00:00Z"},
        {"updated_at": "2999-01-01T00:00:00Z"},
        {"schema_version": 2},
        {"phase": "\x1b[31mspoofed phase"},
    ],
)
def test_invalid_progress_never_announces_a_phase(live_activity, store, change):
    monitor, _controller, notifier, request, reporter = live_activity
    reporter.report("Verifying archive")
    path = reporter._path
    data = json.loads(path.read_text())
    path.write_text(json.dumps(data | change))
    monitor.run_once()
    assert not notifier.calls


@pytest.mark.parametrize("alive", [False, None])
def test_missing_or_dead_progress_never_announces_a_phase(live_activity, monkeypatch, alive):
    monitor, _controller, notifier, _request, reporter = live_activity
    monitor.run_once()
    reporter.report("Verifying archive")
    monkeypatch.setattr("vaultkeep.backup.status.process_alive", lambda *_args: alive)
    monitor.run_once()
    assert not notifier.calls


def test_lost_progress_updates_to_unknown_once_and_recovers(live_activity, store):
    monitor, _controller, notifier, request, reporter = live_activity
    reporter.report("Verifying archive")
    monitor.run_once()
    reporter._path.unlink()
    monitor.run_once()
    monitor.run_once()
    assert notifier.calls[-1] == (
        "VaultKeep Trigger: backup unknown",
        "instance electrum-main: Backup progress unavailable; awaiting result",
    )
    assert len(notifier.calls) == 2
    reporter.heartbeat()
    monitor.run_once()
    assert notifier.calls[-1][1] == "instance electrum-main: Verifying archive"


def test_no_receipt_and_absent_worker_replace_activity_with_unknown_outcome(live_activity):
    monitor, controller, notifier, request, reporter = live_activity
    reporter.report("Verifying archive")
    monitor.run_once()
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor.run_once()
    assert notifier.calls[-1][0] == "VaultKeep Trigger: outcome unknown"
    assert notifier.options[-1] == (request.instance_id, True)
    assert not monitor._last_activity


def test_disabled_notifications_do_not_read_live_progress(live_activity, monkeypatch):
    monitor, _controller, notifier, _request, _reporter = live_activity
    monitor.config = replace(monitor.config, notifications=NotificationConfig(enabled=False))

    def fail(*_args):
        pytest.fail("disabled progress notifications must not read telemetry")

    monkeypatch.setattr("vaultkeep.trigger.monitor.backup_status", fail)
    monitor.run_once()
    assert not notifier.calls


def test_restarted_monitor_displays_current_phase_without_repeating_start(live_activity, store):
    monitor, controller, _notifier, _request, reporter = live_activity
    reporter.report("Verifying archive")
    monitor.run_once()
    notifier = RecordingNotifier()
    restarted = Monitor(
        monitor.config,
        store=store,
        provider=monitor.provider,
        controller=controller,
        notifier=notifier,
    )
    restarted._startup_reconcile()
    restarted.run_once()
    assert len(notifier.calls) == 1
    assert notifier.calls[0][1] == "instance electrum-main: Verifying archive"


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
    return json.dumps(result["safety"])


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
    assert updated is not None and not updated.review_required
    assert not updated.attempt_summaries[-1]["review_required"]
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
    assert updated.review_required
    assert updated.attempt_summaries[-1]["review_required"]


def test_receipt_proves_execution_despite_a_saved_submission_rejection(store):
    instance, request = _saved_attempt(store, outcome=AttemptOutcome.CREATED)
    launch = store.read_launch(request.attempt_id)
    store.write_launch(replace(launch, rejected_before_queue=True))
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor._startup_reconcile()
    assert store.read_instance(instance.id).review_required


def test_final_check_refusal_resolves_only_its_execution_review(store, capsys, caplog):
    instance, request = _saved_attempt(store)
    monitor, controller, _ = _make(store, FakeProvider([_running_proc()]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState(
        "loaded", "activating", "start"
    )
    monitor.run_once()
    assert store.read_instance(instance.id).active_review_required
    store.write_receipt(
        Receipt(
            request.attempt_id,
            request.request_id,
            instance.id,
            AttemptOutcome.BLOCKED,
            utc_now(),
            utc_now(),
            0,
            blocked_reason="final recheck returned running",
        )
    )
    receipt_bytes = store.receipt_path(request.attempt_id).read_bytes()
    with caplog.at_level("INFO", logger="vaultkeep.trigger.monitor"):
        monitor.run_once()
    assert "execution review resolved" in caplog.text
    assert request.attempt_id in caplog.text
    # The worker is still active; only the execution risk has been resolved.
    state = store.read_instance(instance.id)
    assert state.active_attempt_id == request.attempt_id
    assert not state.active_review_required and not state.review_required
    assert state.attempt_summaries[-1]["review_required"]  # Historical annotation retained.
    assert state.attempt_summaries[-1]["reason"] == "final recheck returned running"
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    restarted, _, _ = _make(store, FakeProvider([]), controller=controller, instance=instance)
    restarted._startup_reconcile()
    assert not store.read_instance(instance.id).review_required
    assert _review_status(store, instance.id, capsys, verbose=True) == "[]"
    output = _review_status(store, instance.id, capsys, verbose=True)
    assert output == "[]"
    assert store.receipt_path(request.attempt_id).read_bytes() == receipt_bytes


@pytest.mark.parametrize("older_reason", [None, "unidentified historical warning", "own_attempt"])
def test_blocked_attempt_preserves_unattributed_legacy_warning(store, older_reason):
    instance, request = _saved_attempt(store, outcome=AttemptOutcome.BLOCKED)
    state = store.read_instance(instance.id)
    state.review_required = True
    state.review_reason = (
        older_reason
        if older_reason != "own_attempt"
        else "monitoring gap overlapped possible execution"
    )
    Monitor._flag_review(state, request.attempt_id, "new observation problem")
    store.write_instance(state)
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor._startup_reconcile()
    assert store.read_instance(instance.id).review_required
    assert store.read_instance(instance.id).review_reason == state.review_reason


@pytest.mark.parametrize("aggregate_owner", ["earlier", "current"])
def test_blocked_resolution_preserves_other_attempt_risk(store, capsys, aggregate_owner):
    instance, request = _saved_attempt(store, outcome=AttemptOutcome.BLOCKED)
    earlier = make_request(instance)
    state = store.read_instance(instance.id)
    state.attempt_summaries.append(
        {
            "attempt_id": earlier.attempt_id,
            "request_id": earlier.request_id,
            "outcome": "outcome_unknown",
            "review_required": True,
        }
    )
    state.review_required = True
    owner = earlier.attempt_id if aggregate_owner == "earlier" else request.attempt_id
    state.review_reason = f"attempt {owner}: monitoring gap overlapped possible execution"
    state.active_review_required = True
    state.active_review_reason = f"attempt {request.attempt_id}: observation unavailable"
    store.write_instance(state)
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor._startup_reconcile()
    updated = store.read_instance(instance.id)
    assert updated.review_required
    assert earlier.attempt_id in updated.review_reason
    assert updated.attempt_summaries[0] == state.attempt_summaries[0]
    output = _review_status(store, instance.id, capsys, verbose=True)
    assert earlier.attempt_id in output
    assert request.attempt_id not in output


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
    assert updated.review_required
    assert updated.attempt_summaries[0] == previous_summary
    assert not updated.attempt_summaries[-1]["review_required"]
    output = _review_status(store, instance.id, capsys, verbose=True)
    assert previous.attempt_id in output


def test_delay_and_restart_preserve_risk_while_worker_is_still_running(store):
    instance, request = _saved_attempt(store)
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState(
        "loaded", "activating", "start"
    )
    monitor._last_poll[instance.id] = 0
    monitor._clock = lambda: 10
    monitor.run_once()
    assert store.read_instance(instance.id).review_required
    restarted, _, _ = _make(store, FakeProvider([]), controller=controller, instance=instance)
    restarted._startup_reconcile()
    restarted.run_once()
    assert store.read_instance(instance.id).review_required
    assert store.read_instance(instance.id).active_review_required


def test_contradictory_blocked_receipt_cannot_clear_review(store):
    instance, request = _saved_attempt(store)
    state = store.read_instance(instance.id)
    state.review_required = state.active_review_required = True
    state.review_reason = state.active_review_reason = (
        f"attempt {request.attempt_id}: monitoring gap"
    )
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
    assert store.read_instance(instance.id).review_required


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
    assert store.read_instance(instance.id).review_required


def test_attributable_legacy_observation_warning_can_resolve_without_erasing_history(store):
    instance, request = _saved_attempt(store, outcome=AttemptOutcome.BLOCKED)
    state = store.read_instance(instance.id)
    state.review_required = state.active_review_required = True
    state.review_reason = state.active_review_reason = (
        f"instance observation was running during attempt {request.attempt_id}"
    )
    store.write_instance(state)
    monitor, controller, _ = _make(store, FakeProvider([]), instance=instance)
    controller.states[unit_name_for(request.attempt_id)] = UnitState("loaded", "inactive", "dead")
    monitor._startup_reconcile()
    updated = store.read_instance(instance.id)
    assert not updated.review_required
    assert updated.attempt_summaries[-1]["review_required"]


def test_completed_blocked_summary_cannot_contradict_receipt_and_clear_warning(store):
    instance, request = _saved_attempt(store, outcome=AttemptOutcome.BLOCKED)
    state = store.read_instance(instance.id)
    state.review_required = True
    state.review_reason = f"attempt {request.attempt_id}: monitoring gap"
    state.attempt_summaries.append(
        {
            "attempt_id": request.attempt_id,
            "request_id": request.request_id,
            "outcome": "created",
            "review_required": True,
        }
    )
    store.write_instance(state)
    monitor, _, _ = _make(store, FakeProvider([]), instance=instance)
    monitor._startup_reconcile()
    assert store.read_blocked(instance.id) is not None
    assert store.read_instance(instance.id).review_required


def test_history_cleanup_keeps_unresolved_attempt_identifiable(store, capsys):
    instance = make_instance()
    monitor, _, _ = _make(store, FakeProvider([]), instance=instance)
    state = InstanceState(instance_id=instance.id, review_required=True)
    for index in range(51):
        request = make_request(instance)
        store.write_request(request)
        state.attempt_summaries.append(
            {
                "attempt_id": request.attempt_id,
                "request_id": request.request_id,
                "outcome": "created",
                "review_required": index == 0,
            }
        )
    oldest = state.attempt_summaries[0]["attempt_id"]
    state.review_reason = f"attempt {oldest}: monitoring gap overlapped possible execution"
    store.write_instance(state)
    monitor._cleanup(state)
    assert not store.request_path(oldest).exists()
    updated = store.read_instance(instance.id)
    assert updated.review_required and updated.attempt_summaries[0]["review_required"]
    output = _review_status(store, instance.id, capsys, verbose=True)
    assert oldest in output
    assert "Exact execution interval unavailable" in output
