"""Desktop delivery stays ordered, bounded by timeouts, and best-effort."""

from __future__ import annotations

import subprocess
from threading import Event

import pytest

from vaultkeep.trigger.config import NotificationConfig
from vaultkeep.trigger.notify import NotifySendNotifier, build_notifier


def _wait(notifier: NotifySendNotifier) -> None:
    with notifier._lock:
        worker = notifier._worker
    if worker is not None:
        worker.join(timeout=3)
        assert not worker.is_alive(), "notification worker did not finish"
    assert not notifier._pending


def test_updates_replace_each_instances_notification_and_forget_finished_ids(monkeypatch):
    calls = []
    ids = iter([41, 42, 43, 44, 45, 46])

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, f"{next(ids)}\n".encode(), b"")

    monkeypatch.setattr("vaultkeep.trigger.notify.subprocess.run", run)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True, timeout_ms=250))
    notifier.notify("Starting A", "requested", key="a")
    notifier.notify("Starting B", "requested", key="b")
    notifier.notify("Running A", "Verifying archive", key="a")
    notifier.notify("Finished A", "created", key="a", finished=True)
    notifier.notify("Starting A again", "requested", key="a")
    notifier.notify("Running B", "Verifying archive", key="b")
    _wait(notifier)

    assert [call[0] for call in calls] == [
        ["notify-send", "--print-id", "--expire-time=0", "--", "Starting A", "requested"],
        ["notify-send", "--print-id", "--expire-time=0", "--", "Starting B", "requested"],
        [
            "notify-send",
            "--print-id",
            "--replace-id=41",
            "--expire-time=0",
            "--",
            "Running A",
            "Verifying archive",
        ],
        [
            "notify-send",
            "--print-id",
            "--replace-id=43",
            "--expire-time=-1",
            "--",
            "Finished A",
            "created",
        ],
        ["notify-send", "--print-id", "--expire-time=0", "--", "Starting A again", "requested"],
        [
            "notify-send",
            "--print-id",
            "--replace-id=42",
            "--expire-time=0",
            "--",
            "Running B",
            "Verifying archive",
        ],
    ]
    assert all(
        options == {"capture_output": True, "timeout": 0.25, "check": False}
        for _argv, options in calls
    )


def test_slow_delivery_does_not_block_monitor_or_reorder_completion(monkeypatch):
    started, release = Event(), Event()
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if len(calls) == 1:
            started.set()
            assert release.wait(timeout=3)
        return subprocess.CompletedProcess(argv, 0, b"73\n", b"")

    monkeypatch.setattr("vaultkeep.trigger.notify.subprocess.run", run)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True))
    try:
        notifier.notify("Starting", "requested", key="job")
        assert started.wait(timeout=3)
        notifier.notify("Running", "Verifying archive", key="job")
        notifier.notify("Finished", "created", key="job", finished=True)
        notifier.notify("Warning", "review required")
        assert len(calls) == 1
    finally:
        release.set()
        _wait(notifier)
    assert [argv[-2] for argv in calls] == ["Starting", "Running", "Finished", "Warning"]
    assert calls[-1] == ["notify-send", "Warning", "review required"]
    assert notifier._notification_ids == {}


@pytest.mark.parametrize(
    "failure",
    [
        OSError("no session bus"),
        subprocess.TimeoutExpired("notify-send", 0.25),
        b"invalid",
        b"",
        b"0",
        b"-1",
        b"4294967296",
        b"\xff",
        None,
    ],
)
def test_delivery_failure_does_not_break_subsequent_updates(monkeypatch, failure):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if len(calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return subprocess.CompletedProcess(argv, 1 if failure is None else 0, failure, b"")
        return subprocess.CompletedProcess(argv, 0, b"73\n", b"")

    monkeypatch.setattr("vaultkeep.trigger.notify.subprocess.run", run)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True))
    notifier.notify("Starting", "requested", key="job")
    notifier.notify("Running", "Verifying archive", key="job")
    notifier.notify("Finished", "created", key="job", finished=True)
    _wait(notifier)
    assert not any(arg.startswith("--replace-id") for arg in calls[1])
    assert "--replace-id=73" in calls[2]
    assert notifier._notification_ids == {}


def test_custom_command_preserves_two_argument_interface(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("vaultkeep.trigger.notify.subprocess.run", run)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True, command="/opt/notify-wrapper"))
    notifier.notify("Running", "Verifying archive", key="job")
    notifier.notify("Finished", "created", key="job", finished=True)
    _wait(notifier)
    assert calls == [
        ["/opt/notify-wrapper", "Running", "Verifying archive"],
        ["/opt/notify-wrapper", "Finished", "created"],
    ]


def test_absolute_notify_send_path_supports_replacement(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"73\n", b"")

    monkeypatch.setattr("vaultkeep.trigger.notify.subprocess.run", run)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True, command="/usr/bin/notify-send"))
    notifier.notify("Starting", "requested", key="job")
    notifier.notify("Running", "Verifying archive", key="job")
    _wait(notifier)
    assert calls[0][0] == "/usr/bin/notify-send"
    assert "--replace-id=73" in calls[1]


def test_disabled_notifications_do_not_start_delivery(monkeypatch):
    def fail(*_args, **_kwargs):
        pytest.fail("disabled notifications must not start a thread")

    monkeypatch.setattr("vaultkeep.trigger.notify.threading.Thread", fail)
    config = NotificationConfig(enabled=False)
    build_notifier(config).notify("Running", "Verifying archive", key="job")
    NotifySendNotifier(config).notify("Running", "Verifying archive", key="job")


def test_thread_start_failure_is_best_effort(monkeypatch):
    def fail(_thread):
        raise RuntimeError("cannot start a new thread")

    monkeypatch.setattr("vaultkeep.trigger.notify.threading.Thread.start", fail)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True))
    notifier.notify("Running", "Verifying archive", key="job")
    assert notifier._worker is None
    assert not notifier._pending
