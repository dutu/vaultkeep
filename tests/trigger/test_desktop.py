"""Discover a live desktop bus without trusting another account's environment."""

from __future__ import annotations

import os
import struct
import subprocess
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from vaultkeep.trigger import desktop


def process(*, address="unix:path=/tmp/desktop", display=":1", uid=None):
    uid = os.geteuid() if uid is None else uid
    uids = SimpleNamespace(real=uid, effective=uid)
    result = Mock()
    result.info = {"uids": uids}
    result.uids.return_value = uids
    result.environ.return_value = {"DISPLAY": display, "DBUS_SESSION_BUS_ADDRESS": address}
    result.is_running.return_value = True
    return result


@pytest.fixture
def no_inherited_bus(monkeypatch):
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(desktop, "runtime_dir", lambda: None)


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("unix:path=/tmp/dbus-test,guid=abc", ("unix:path=/tmp/dbus-test", "/tmp/dbus-test")),
        ("unix:path=/tmp/a%2Cb", ("unix:path=/tmp/a%2Cb", "/tmp/a,b")),
        ("unix:abstract=/tmp/dbus-test", ("unix:abstract=/tmp/dbus-test", "\0/tmp/dbus-test")),
        ("tcp:host=localhost,port=123", None),
        ("unix:path=relative", None),
        ("unix:path=/a;unix:path=/b", None),
        ("unix:path=/a,abstract=b", None),
        ("unix:path=/a,path=/b", None),
        ("unix:path=/a,unsupported=b", None),
        ("unix:path=/a,b", None),
        ("unix:path=", None),
        ("unix:path=/a%00b", None),
        ("unix:path=/" + "a" * 4096, None),
    ],
)
def test_only_single_local_unix_addresses_are_supported(address, expected):
    assert desktop._local_address(address) == expected


def test_inherited_working_bus_requires_no_process_scan(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/desktop,guid=abc")
    monkeypatch.setattr(desktop, "_notification_service", lambda address, deadline: "running")

    def fail(_deadline):
        pytest.fail("a working configured bus needs no discovery")

    monkeypatch.setattr(desktop, "_desktop_addresses", fail)
    assert desktop.notification_bus(monotonic() + 2) == "unix:path=/tmp/desktop"


def test_standard_user_bus_is_used_before_process_discovery(
    no_inherited_bus, monkeypatch, tmp_path
):
    monkeypatch.setattr(desktop, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(desktop, "_notification_service", lambda address, deadline: "running")
    assert desktop.notification_bus(monotonic() + 2) == f"unix:path={tmp_path}/bus"


def test_running_private_desktop_precedes_a_generic_activatable_user_bus(
    no_inherited_bus,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(desktop, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(desktop, "_desktop_addresses", lambda deadline: {"unix:path=/tmp/private"})
    monkeypatch.setattr(
        desktop,
        "_notification_service",
        lambda address, deadline: (
            "running" if address == "unix:path=/tmp/private" else "activatable"
        ),
    )
    assert desktop.notification_bus(monotonic() + 2) == "unix:path=/tmp/private"


@pytest.mark.parametrize("candidates", [None, set()])
def test_configured_bus_can_activate_notifications_without_process_discovery(
    monkeypatch, candidates
):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/desktop")
    monkeypatch.setattr(desktop, "runtime_dir", lambda: None)
    monkeypatch.setattr(desktop, "_desktop_addresses", lambda deadline: candidates)
    monkeypatch.setattr(desktop, "_notification_service", lambda address, deadline: "activatable")
    assert desktop.notification_bus(monotonic() + 2) == "unix:path=/tmp/desktop"


def test_nomachine_bus_is_discovered_despite_a_stale_inherited_address(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/stale")
    monkeypatch.setattr(desktop, "runtime_dir", lambda: None)
    gui = process(address="unix:path=/tmp/dbus-NoMachine,guid=abc")
    monkeypatch.setattr(desktop.psutil, "process_iter", lambda attrs: [gui, gui])
    checked = []

    def probe(address, deadline):
        checked.append(address)
        return "running" if address == "unix:path=/tmp/dbus-NoMachine" else None

    monkeypatch.setattr(desktop, "_notification_service", probe)
    assert desktop.notification_bus(monotonic() + 2) == "unix:path=/tmp/dbus-NoMachine"
    assert checked == ["unix:path=/tmp/stale", "unix:path=/tmp/dbus-NoMachine"]


def test_other_users_and_unreadable_or_replaced_processes_are_ignored(monkeypatch):
    foreign = process(uid=os.geteuid() + 1)
    inaccessible = process()
    inaccessible.environ.side_effect = psutil.AccessDenied(12)
    gone = process()
    gone.environ.side_effect = psutil.NoSuchProcess(13)
    reused = process()
    reused.is_running.return_value = False
    changed_owner = process()
    changed_owner.uids.return_value = SimpleNamespace(effective=os.geteuid() + 1)
    ssh = process(display="")
    wayland = process()
    wayland.environ.return_value = {
        "WAYLAND_DISPLAY": "wayland-0",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/wayland",
    }
    monkeypatch.setattr(
        desktop.psutil,
        "process_iter",
        lambda attrs: [foreign, inaccessible, gone, reused, changed_owner, ssh, wayland],
    )
    assert desktop._desktop_addresses(monotonic() + 2) == {"unix:path=/tmp/wayland"}
    foreign.environ.assert_not_called()


@pytest.mark.parametrize("live", [set(), {"unix:path=/tmp/a", "unix:path=/tmp/b"}])
def test_missing_or_ambiguous_desktops_do_not_choose_an_arbitrary_bus(
    no_inherited_bus,
    monkeypatch,
    caplog,
    live,
):
    monkeypatch.setattr(desktop, "_desktop_addresses", lambda deadline: live)
    monkeypatch.setattr(desktop, "_notification_service", lambda address, deadline: "running")
    assert desktop.notification_bus(monotonic() + 2) is None
    assert (
        "multiple graphical session buses" in caplog.text
        if live
        else "no accessible" in caplog.text
    )


def test_partial_discovery_on_timeout_or_snapshot_failure_is_not_used(
    no_inherited_bus, monkeypatch
):
    gui = process()
    monkeypatch.setattr(desktop.psutil, "process_iter", lambda attrs: [gui])
    assert desktop.notification_bus(monotonic() - 1) is None
    gui.environ.assert_not_called()

    def fail(_attrs):
        raise psutil.AccessDenied(12)

    monkeypatch.setattr(desktop.psutil, "process_iter", fail)
    assert desktop.notification_bus(monotonic() + 2) is None


@pytest.fixture
def bus_peer(monkeypatch):
    peer = Mock()
    peer.getsockopt.return_value = struct.pack("3i", 123, os.geteuid(), os.getegid())
    context = Mock()
    context.__enter__ = Mock(return_value=peer)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(desktop.socket, "socket", lambda *args: context)
    return peer


def test_bus_peer_must_belong_to_current_user_before_probing(monkeypatch, bus_peer):
    bus_peer.getsockopt.return_value = struct.pack("3i", 123, os.geteuid() + 1, os.getegid())

    def fail(*args, **kwargs):
        pytest.fail("must not query a foreign user's bus")

    monkeypatch.setattr(desktop.subprocess, "run", fail)
    assert not desktop._notification_service("unix:path=/tmp/foreign", monotonic() + 2)


@pytest.mark.parametrize("activatable", [False, True])
def test_probe_checks_the_notification_service_without_activating_it(
    monkeypatch, bus_peer, activatable
):
    calls = []

    def run(argv, **options):
        calls.append((argv, options))
        output = b"b true\n" if not activatable else b"b false\n"
        if argv[-1] == "ListActivatableNames":
            output = b'as 1 "org.freedesktop.Notifications"\n'
        return subprocess.CompletedProcess(argv, 0, output, b"")

    monkeypatch.setattr(desktop.subprocess, "run", run)
    assert desktop._notification_service("unix:abstract=/tmp/desktop", monotonic() + 2) == (
        "activatable" if activatable else "running"
    )
    bus_peer.connect.assert_called_once_with("\0/tmp/desktop")
    assert len(calls) == (2 if activatable else 1)
    for argv, options in calls:
        assert "--auto-start=no" in argv and "--allow-interactive-authorization=no" in argv
        assert "--address=unix:abstract=/tmp/desktop" in argv
        assert 0 < options["timeout"] <= 0.5


@pytest.mark.parametrize(
    "failure", [FileNotFoundError("busctl"), subprocess.TimeoutExpired("busctl", 0.5)]
)
def test_unavailable_probe_is_best_effort(monkeypatch, bus_peer, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(desktop.subprocess, "run", fail)
    assert not desktop._notification_service("unix:path=/tmp/desktop", monotonic() + 2)
