"""Root/user launcher lifecycle and privilege boundaries of installer maintenance."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaultkeep.maintenance import read_file


@pytest.fixture
def helper(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "install_maintenance", scripts / "install_maintenance.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_empty_system_and_user_launcher_inventories_can_pause_and_resume(
    helper, monkeypatch, tmp_path
):
    monkeypatch.setattr(helper, "user_managers", lambda: [("alice", 1000)])
    monkeypatch.setattr(helper.pwd, "getpwall", lambda: [])
    monkeypatch.setattr(
        helper.pwd, "getpwnam", lambda account: SimpleNamespace(pw_dir=str(tmp_path / account))
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "list-unit-files" in argv:
            # systemd 257 exits 1 without diagnostics when no unit files match.
            return subprocess.CompletedProcess(argv, 1, "", "")
        assert "show-environment" in argv or "list-units" in argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(helper.subprocess, "run", run)
    layout = SimpleNamespace(
        service=tmp_path / "system/vaultkeep@.service",
        config=tmp_path / "etc",
        registry=tmp_path / "registry",
    )
    journal = tmp_path / "journal.json"

    helper.pause(journal, layout)
    helper.resume(journal)

    specs = json.loads(journal.read_text())["accounts"]
    assert [spec["account"] for spec in specs] == [None, "alice"]
    assert all(spec["active"] == [] and spec["states"] == {} for spec in specs)
    listings = [argv for argv in calls if "list-unit-files" in argv]
    assert len(listings) == 2
    assert listings[0][0] == "systemctl"
    assert listings[1][:3] == ["runuser", "-u", "alice"]


def test_user_launchers_preserve_all_activity_enablement_combinations(
    helper, monkeypatch, tmp_path
):
    root = tmp_path / "user-units"
    root.mkdir()
    units = [f"vaultkeep-trigger@job-{index}.service" for index in range(4)]
    states = {}
    for index, unit in enumerate(units):
        path = root / unit
        path.write_text(f"# Managed by Vaultkeep: {unit}\n")
        states[unit] = {
            "LoadState": "loaded",
            "ActiveState": "active" if index % 2 == 0 else "inactive",
            "SubState": "running" if index % 2 == 0 else "dead",
            "FragmentPath": str(path),
            "DropInPaths": "",
            "UnitFileState": "enabled-runtime" if index < 2 else "disabled",
        }
    monkeypatch.setattr(helper, "user_managers", lambda: [("alice", 1000)])
    monkeypatch.setattr(helper.pwd, "getpwall", lambda: [])
    monkeypatch.setattr(
        helper,
        "account_spec",
        lambda account, uid, layout: {
            "account": account,
            "uid": uid,
            "command": ["user"] if account else ["root"],
            "unit_roots": [str(root)],
            "active": [],
            "states": {},
        },
    )
    monkeypatch.setattr(helper, "properties", lambda command, unit: dict(states[unit]))
    journal = tmp_path / "journal.json"
    actions = []

    def execute(argv, **kwargs):
        if argv[1] in ("list-units", "list-unit-files"):
            return "\n".join(f"{unit} loaded" for unit in units) if argv[0] == "user" else ""
        action, unit = argv[1:]
        # Resume metadata must be durable before the first stop.
        assert json.loads(journal.read_text())["accounts"][1]["active"] == [units[0], units[2]]
        actions.append((action, unit))
        states[unit]["ActiveState"] = "active" if action == "start" else "inactive"
        return ""

    monkeypatch.setattr(helper, "execute", execute)
    layout = SimpleNamespace(timer=tmp_path / "vaultkeep@.timer")
    helper.pause(journal, layout)
    assert actions == [("stop", units[0]), ("stop", units[2])]
    helper.resume(journal)
    assert actions[2:] == [("start", units[0]), ("start", units[2])]
    assert [states[unit]["UnitFileState"] for unit in units] == [
        "enabled-runtime",
        "enabled-runtime",
        "disabled",
        "disabled",
    ]


def test_resume_attempts_remaining_user_units_after_a_failure(helper, monkeypatch, tmp_path):
    journal = tmp_path / "journal.json"
    units = ["vaultkeep@a.timer", "vaultkeep@b.timer"]
    journal.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account": "alice",
                        "command": ["user"],
                        "active": units,
                        "states": dict.fromkeys(units, "disabled"),
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(
        helper,
        "properties",
        lambda *args: {
            "UnitFileState": "disabled",
            "ActiveState": "active",
        },
    )
    calls = []

    def execute(argv):
        calls.append(argv[-1])
        if argv[-1] == units[0]:
            raise ValueError("user manager rejected start")
        return ""

    monkeypatch.setattr(helper, "execute", execute)
    with pytest.raises(ValueError, match=r"alice.*a\.timer"):
        helper.resume(journal)
    assert calls == units
    assert journal.exists()


def test_offline_accounts_are_discovered_without_starting_a_manager(helper, monkeypatch, tmp_path):
    home = tmp_path / "alice"
    root = home / ".config/systemd/user"
    root.mkdir(parents=True)
    (root / "vaultkeep-trigger@job-identity.service").touch()
    entry = SimpleNamespace(pw_dir=str(home), pw_name="alice")
    monkeypatch.setattr(helper.pwd, "getpwall", lambda: [entry])
    monkeypatch.setattr(helper.pwd, "getpwnam", lambda account: entry)
    monkeypatch.setattr(helper, "user_managers", lambda: [])
    calls = []
    monkeypatch.setattr(helper, "execute", lambda argv, **kwargs: calls.append(argv) or "")
    layout = SimpleNamespace(
        service=tmp_path / "system/vaultkeep@.service",
        config=tmp_path / "etc",
        registry=tmp_path / "registry",
        timer=tmp_path / "system/vaultkeep@.timer",
    )
    document = helper.inventory(layout)
    alice = document["accounts"][1]
    assert alice["account"] == "alice"
    assert alice["command"] is None and alice["active"] == []
    assert alice["unit_roots"] == [str(root)]
    assert all(argv[0] == "systemctl" and argv[1].startswith("list-") for argv in calls)


def test_configuration_check_collects_all_accounts_even_after_an_account_failure(
    helper, monkeypatch, tmp_path
):
    specs = [{"account": name} for name in (None, "alice", "bob", "carol")]
    monkeypatch.setattr(helper, "inventory", lambda layout: {"accounts": specs})
    calls = []

    def operation(spec, python, action, payload):
        calls.append(spec["account"])
        assert python == "/candidate/venv/bin/python"
        assert action == "check-configurations"
        assert payload["user"] == (spec["account"] is not None)
        if spec["account"] == "bob":
            raise ValueError("bob: permission denied inspecting jobs")
        name = spec["account"] or "system"
        return {
            "errors": [
                {
                    "job": name,
                    "file": str(tmp_path / f"{name}.yaml"),
                    "issues": [
                        {"path": "trigger.notifications", "message": "Unknown property."},
                        {"path": "destination.root", "message": "Field required"},
                    ],
                }
            ]
        }

    monkeypatch.setattr(helper, "account_operation", operation)
    with pytest.raises(ValueError) as caught:
        helper.check_configurations(None, "/candidate/venv/bin/python")

    assert calls == [None, "alice", "bob", "carol"]
    message = str(caught.value)
    assert "Found 3 invalid job configurations:" in message
    for name in ("system", "alice", "carol"):
        assert f"Job: {name}" in message
        assert f"File: {tmp_path / f'{name}.yaml'}" in message
    assert "User: system" not in message
    assert "User: alice" in message and "User: carol" in message
    assert "bob: permission denied" in message
    assert message.count("trigger.notifications") == 3
    assert message.count("destination.root") == 3


def test_account_rendering_uses_own_home_and_manager_xdg_environment(helper, monkeypatch, tmp_path):
    monkeypatch.setattr(
        helper.pwd, "getpwnam", lambda account: SimpleNamespace(pw_dir=str(tmp_path))
    )
    monkeypatch.setattr(
        helper,
        "execute",
        lambda argv: (
            f"XDG_CONFIG_HOME={tmp_path}/custom\nXDG_STATE_HOME={tmp_path}/state\n"
            "PYTHONPATH=/untrusted\n"
        ),
    )
    spec = helper.account_spec("alice", 1000, None)
    assert spec["env"]["HOME"] == str(tmp_path)
    assert spec["paths"]["units_root"] == str(tmp_path / "custom/systemd/user")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"changes": [], "jobs": []}', "")

    monkeypatch.setattr(helper.subprocess, "run", run)
    helper.account_operation(spec, "/opt/vaultkeep/current/venv/bin/python", "plan", {})
    argv = calls[0]
    assert argv[:6] == ["runuser", "-u", "alice", "--", "env", "-i"]
    assert f"HOME={tmp_path}" in argv
    assert f"XDG_CONFIG_HOME={tmp_path}/custom" in argv
    assert not any("PYTHONPATH=" in argument for argument in argv)
    assert argv[-4:] == ["-I", "-m", "vaultkeep.maintenance", "plan"]


@pytest.mark.parametrize(
    "encoded,expected",
    [
        ("/home/alice/.config", "/home/alice/.config"),
        ("$'/home/alice/config with spaces'", "/home/alice/config with spaces"),
        (r"$'/home/alice/it\'s literal'", "/home/alice/it's literal"),
        (r"$'/home/alice/a\\b'", "/home/alice/a\\b"),
        ("$'/home/alice/é$(touch /tmp/unused)'", "/home/alice/é$(touch /tmp/unused)"),
        ('""', ""),
    ],
)
def test_xdg_environment_shell_quoting_is_decoded_as_data(helper, encoded, expected):
    assert helper.environment_value(encoded) == expected


def test_job_restoration_does_not_need_a_working_candidate_interpreter(helper, tmp_path):
    path = tmp_path / "unit.service"
    path.write_text("old contents\n")
    original = read_file(path)
    path.unlink()
    spec = {"account": None}
    helper.account_operation(
        spec,
        "/missing/candidate/python",
        "restore",
        {
            "changes": [
                {
                    "path": str(path),
                    "before": original,
                    "after": None,
                }
            ]
        },
    )
    assert path.read_text() == "old contents\n"
