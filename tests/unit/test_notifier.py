"""Desktop delivery stays ordered, bounded by timeouts, and best-effort."""

from __future__ import annotations

import subprocess
from threading import Event
from time import monotonic, sleep

import pytest

from vaultkeep.notifications.notifier import NotificationConfig, NotifySendNotifier, build_notifier


def test_shutdown_delivers_final_after_inflight_start_without_obsolete_progress(monkeypatch):
    started, release = Event(), Event()
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if len(calls) == 1:
            started.set()
            assert release.wait(2)
        return subprocess.CompletedProcess(argv, 0, b"73\n", b"")

    monkeypatch.setattr("vaultkeep.notifications.notifier.subprocess.run", run)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True))
    try:
        notifier.notify("Started", "start", key="backup:exec")
        assert started.wait(2)
        for number in range(100):
            notifier.notify("Running", str(number), key="backup:exec", replace_pending=True)
        assert len(notifier._pending) == 1
        notifier.notify("Completed", "created", key="backup:exec", finished=True)
    finally:
        release.set()
        notifier.close()
    assert [argv[-2] for argv in calls] == ["Started", "Completed"]
    assert "--replace-id=73" in calls[-1]
    assert not notifier._pending
    notifier.notify("Late", "ignored")
    assert len(calls) == 2


def test_short_lived_delivery_has_a_total_shutdown_bound(monkeypatch):
    started = Event()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        started.set()
        sleep(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("vaultkeep.notifications.notifier.subprocess.run", run)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True, timeout_ms=40))
    notifier.notify("Started", "start", key="backup:exec")
    assert started.wait(2)
    notifier.notify("Completed", "created", key="backup:exec", finished=True)
    start = monotonic()
    notifier.close()
    assert monotonic() - start < 0.5
    _wait(notifier)
    assert [argv[-2] for argv in calls] == ["Started", "Completed"]


@pytest.fixture(autouse=True)
def desktop_bus(monkeypatch):
    monkeypatch.setattr(
        "vaultkeep.notifications.notifier.notification_bus",
        lambda _deadline: "unix:path=/tmp/test-desktop",
    )


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

    monkeypatch.setattr("vaultkeep.notifications.notifier.subprocess.run", run)
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
            "--expire-time=0",
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
    for _argv, options in calls:
        assert options["capture_output"] is True
        assert options["check"] is False
        assert 0 < options["timeout"] <= 0.25
        assert options["env"]["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/test-desktop"


def test_slow_delivery_does_not_block_monitor_or_reorder_completion(monkeypatch):
    started, release = Event(), Event()
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if len(calls) == 1:
            started.set()
            assert release.wait(timeout=3)
        return subprocess.CompletedProcess(argv, 0, b"73\n", b"")

    monkeypatch.setattr("vaultkeep.notifications.notifier.subprocess.run", run)
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

    monkeypatch.setattr("vaultkeep.notifications.notifier.subprocess.run", run)
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

    monkeypatch.setattr("vaultkeep.notifications.notifier.subprocess.run", run)
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

    monkeypatch.setattr("vaultkeep.notifications.notifier.subprocess.run", run)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True, command="/usr/bin/notify-send"))
    notifier.notify("Starting", "requested", key="job")
    notifier.notify("Running", "Verifying archive", key="job")
    _wait(notifier)
    assert calls[0][0] == "/usr/bin/notify-send"
    assert "--replace-id=73" in calls[1]


def test_disabled_notifications_do_not_start_delivery(monkeypatch):
    def fail(*_args, **_kwargs):
        pytest.fail("disabled notifications must not start a thread")

    monkeypatch.setattr("vaultkeep.notifications.notifier.threading.Thread", fail)
    config = NotificationConfig(enabled=False)
    build_notifier(config).notify("Running", "Verifying archive", key="job")
    NotifySendNotifier(config).notify("Running", "Verifying archive", key="job")


def test_thread_start_failure_is_best_effort(monkeypatch):
    def fail(_thread):
        raise RuntimeError("cannot start a new thread")

    monkeypatch.setattr("vaultkeep.notifications.notifier.threading.Thread.start", fail)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True))
    notifier.notify("Running", "Verifying archive", key="job")
    assert notifier._worker is None
    assert not notifier._pending


def test_desktop_changes_clear_replacement_ids_and_override_stale_service_environment(monkeypatch):
    addresses = iter(
        ["unix:path=/tmp/desktop-a", "unix:path=/tmp/desktop-a", "unix:path=/tmp/desktop-b"]
    )
    monkeypatch.setattr(
        "vaultkeep.notifications.notifier.notification_bus", lambda _deadline: next(addresses)
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/stale-desktop")
    calls = []

    def run(argv, **options):
        calls.append((argv, options["env"]["DBUS_SESSION_BUS_ADDRESS"]))
        return subprocess.CompletedProcess(argv, 0, b"73\n", b"")

    monkeypatch.setattr("vaultkeep.notifications.notifier.subprocess.run", run)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True))
    notifier.notify("Starting", "requested", key="job")
    notifier.notify("Running", "Creating and compressing archive", key="job")
    notifier.notify("Running", "Verifying archive", key="job")
    _wait(notifier)
    assert [address for _argv, address in calls] == [
        "unix:path=/tmp/desktop-a",
        "unix:path=/tmp/desktop-a",
        "unix:path=/tmp/desktop-b",
    ]
    assert "--replace-id=73" in calls[1][0]
    assert not any(arg.startswith("--replace-id=") for arg in calls[2][0])


def test_unavailable_desktop_does_not_send_and_delivery_recovers_without_restart(monkeypatch):
    addresses = iter(["unix:path=/tmp/desktop-a", None, "unix:path=/tmp/desktop-b"])
    monkeypatch.setattr(
        "vaultkeep.notifications.notifier.notification_bus", lambda _deadline: next(addresses)
    )
    calls = []

    def run(argv, **_options):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"73\n", b"")

    monkeypatch.setattr("vaultkeep.notifications.notifier.subprocess.run", run)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True))
    notifier.notify("Starting", "requested", key="job")
    notifier.notify("Running", "Creating and compressing archive", key="job")
    notifier.notify("Finished", "created", key="job", finished=True)
    _wait(notifier)
    assert len(calls) == 2
    assert not any(arg.startswith("--replace-id=") for arg in calls[1])
    assert not notifier._notification_ids


def test_custom_commands_do_not_discover_or_override_desktop_environment(monkeypatch):
    def fail(_deadline):
        pytest.fail("custom commands must retain their own environment")

    monkeypatch.setattr("vaultkeep.notifications.notifier.notification_bus", fail)
    calls = []

    def run(argv, **options):
        calls.append(options["env"])
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("vaultkeep.notifications.notifier.subprocess.run", run)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True, command="/opt/notify-wrapper"))
    notifier.notify("Running", "Verifying archive", key="job")
    _wait(notifier)
    assert calls == [None]


def test_failed_delivery_logs_a_bounded_single_line_reason(monkeypatch, caplog):
    def run(argv, **_options):
        return subprocess.CompletedProcess(
            argv, 1, b"", b"Session bus unavailable\n\x1b" + b"x" * 1000
        )

    monkeypatch.setattr("vaultkeep.notifications.notifier.subprocess.run", run)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True))
    notifier.notify("Running", "Verifying archive", key="job")
    _wait(notifier)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "command failed (exit 1): Session bus unavailable" in message
    assert len(message) < 500 and "\n" not in message
    assert message.isprintable()


def test_discovery_and_delivery_share_one_timeout_budget(monkeypatch, caplog):
    clock = iter([0.0, 0.0, 2.5])
    monkeypatch.setattr("vaultkeep.notifications.notifier.monotonic", lambda: next(clock))

    def fail(*args, **kwargs):
        pytest.fail("a notification must not start after its total timeout has elapsed")

    monkeypatch.setattr("vaultkeep.notifications.notifier.subprocess.run", fail)
    notifier = NotifySendNotifier(NotificationConfig(enabled=True, timeout_ms=1000))
    notifier.notify("Finished", "created", key="job", finished=True)
    _wait(notifier)
    assert "deadline exceeded" in caplog.text
    assert not notifier._notification_ids
