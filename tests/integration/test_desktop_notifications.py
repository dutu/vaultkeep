"""Real D-Bus delivery across disposable desktop-session changes."""

from __future__ import annotations

import json
import os
import select
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

from vaultkeep.trigger import desktop
from vaultkeep.trigger.config import NotificationConfig
from vaultkeep.trigger.notify import NotifySendNotifier

SERVER = r"""
import json, os, sys
import dbus, dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
DBusGMainLoop(set_as_default=True)
connection = dbus.bus.BusConnection(os.environ['DBUS_SESSION_BUS_ADDRESS'])
name = dbus.service.BusName('org.freedesktop.Notifications', connection)
class Notifications(dbus.service.Object):
    @dbus.service.method('org.freedesktop.Notifications',
                         in_signature='susssasa{sv}i', out_signature='u')
    def Notify(self, app, replace_id, icon, summary, body, actions, hints, expiration):
        with open(sys.argv[1], 'a') as output:
            record = {'replace': int(replace_id), 'summary': str(summary), 'body': str(body),
                      'expiration': int(expiration)}
            output.write(json.dumps(record) + '\n')
        return dbus.UInt32(replace_id or 81)
service = Notifications(connection, '/org/freedesktop/Notifications')
print('ready', flush=True)
GLib.MainLoop().run()
"""

CLIENT = r"""#!/usr/bin/python3
import os, sys, dbus
args = sys.argv[1:]
replace = next((int(arg.split('=', 1)[1]) for arg in args if arg.startswith('--replace-id=')), 0)
expiration = next((int(arg.split('=', 1)[1]) for arg in args
                   if arg.startswith('--expire-time=')), -1)
connection = dbus.bus.BusConnection(os.environ['DBUS_SESSION_BUS_ADDRESS'])
service = connection.get_object('org.freedesktop.Notifications', '/org/freedesktop/Notifications')
result = service.Notify('Vaultkeep', dbus.UInt32(replace), '', args[-2], args[-1],
    dbus.Array([], signature='s'), dbus.Dictionary({}, signature='sv'), dbus.Int32(expiration),
    dbus_interface='org.freedesktop.Notifications')
print(int(result))
"""


def ready(process):
    assert select.select([process.stdout], [], [], 5)[0], "helper startup timed out"
    line = process.stdout.readline().strip()
    assert line, process.stderr.read().decode()
    return line


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def wait(notifier):
    with notifier._lock:
        worker = notifier._worker
    if worker is not None:
        worker.join(timeout=6)
        assert not worker.is_alive(), "notification delivery timed out"


def test_real_notification_delivery_survives_desktop_session_replacement(monkeypatch):
    if not all(shutil.which(tool) for tool in ("dbus-daemon", "busctl")):
        pytest.skip("real D-Bus tools are unavailable")
    system_python = Path("/usr/bin/python3")
    if not system_python.exists():
        pytest.skip("system Python is required for the disposable desktop helper")
    bindings = subprocess.run(
        [str(system_python), "-c", "import dbus; from gi.repository import GLib"],
        capture_output=True,
        timeout=5,
        check=False,
    )
    if bindings.returncode:
        pytest.skip("optional system python3-dbus/python3-gi bindings are unavailable")
    try:
        with (
            tempfile.TemporaryDirectory(prefix="vk-notify-probe-") as directory,
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer,
        ):
            peer.bind(str(Path(directory) / "probe"))
    except PermissionError:
        pytest.skip("sandbox blocks Unix sockets; run this integration test outside the sandbox")

    processes = []
    session_pids = set()
    original_process_iter = desktop.psutil.process_iter
    monkeypatch.setattr(
        desktop.psutil,
        "process_iter",
        lambda attrs: (
            process for process in original_process_iter(attrs) if process.pid in session_pids
        ),
    )
    try:
        with tempfile.TemporaryDirectory(prefix="vk-notification-check-") as directory:
            root = Path(directory)
            monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
            monkeypatch.setenv("XDG_RUNTIME_DIR", directory)
            client = root / "notify-send"
            client.write_text(CLIENT)
            client.chmod(0o700)
            log = root / "notifications.jsonl"

            def session(number):
                daemon = subprocess.Popen(
                    [
                        "dbus-daemon",
                        "--session",
                        "--nofork",
                        "--print-address=1",
                        f"--address=unix:path={root}/desktop-{number}",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                processes.append(daemon)
                address = ready(daemon).decode()
                gui = subprocess.Popen(
                    ["/usr/bin/python3", "-u", "-c", SERVER, str(log)],
                    env=dict(os.environ, DBUS_SESSION_BUS_ADDRESS=address, DISPLAY=f":{number}"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                processes.append(gui)
                assert ready(gui) == b"ready"
                session_pids.add(gui.pid)
                return daemon, gui, address

            daemon, gui, old_address = session(91)
            notifier = NotifySendNotifier(NotificationConfig(enabled=True, command=str(client)))
            notifier.notify("Starting", "Creating and compressing archive", key="job")
            notifier.notify("Running", "Verifying archive", key="job")
            wait(notifier)
            records = [json.loads(line) for line in log.read_text().splitlines()]
            assert [record["replace"] for record in records] == [0, 81], records

            stop(gui)
            stop(daemon)
            session_pids.discard(gui.pid)
            monkeypatch.setenv(
                "DBUS_SESSION_BUS_ADDRESS", old_address
            )  # Stale service environment.
            daemon, gui, _new_address = session(92)
            notifier.notify("Running", "Verifying archive checksum", key="job")
            notifier.notify("Finished", "New archive created", key="job", finished=True)
            wait(notifier)
            records = [json.loads(line) for line in log.read_text().splitlines()]
            assert [record["replace"] for record in records] == [0, 81, 0, 81], records
            assert records[-1]["body"] == "New archive created"
            assert all(record["expiration"] == 0 for record in records)
            assert not notifier._notification_ids
    finally:
        for process in reversed(processes):
            stop(process)
