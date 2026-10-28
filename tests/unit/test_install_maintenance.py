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
    monkeypatch.setattr(helper, "check_activity", lambda *args: None)

    helper.pause(journal, layout)
    helper.resume(journal)

    specs = json.loads(journal.read_text())["accounts"]
    assert [spec["account"] for spec in specs] == [None, "alice"]
    assert all(spec["active"] == [] and spec["states"] == {} for spec in specs)
    listings = [argv for argv in calls if "list-unit-files" in argv]
    assert len(listings) == 2
    assert listings[0][0] == "systemctl"
    assert listings[1][:3] == ["runuser", "-u", "alice"]


def test_summary_lists_owners_instances_and_automation_outcomes(helper, tmp_path, capsys):
    journal = tmp_path / "journal.json"
    journal.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "account": None,
                        "active": ["vaultkeep@documents.timer"],
                        "plan": {"jobs": [{"unit": "vaultkeep@documents.timer", "monitor": False}]},
                    },
                    {
                        "account": "alice",
                        "active": ["vaultkeep-trigger@wallet-scope123.service"],
                        "plan": {
                            "jobs": [
                                {
                                    "unit": "vaultkeep-trigger@wallet-scope123.service",
                                    "monitor": True,
                                },
                                {
                                    "unit": "vaultkeep-trigger@paused-scope456.service",
                                    "monitor": True,
                                },
                                {"unit": "vaultkeep@documents.timer", "monitor": False},
                                {
                                    "unit": "vaultkeep@archive-repo-nas-abc123.timer",
                                    "monitor": False,
                                },
                                {
                                    "unit": "vaultkeep@archive-repo-usb-def456.timer",
                                    "monitor": False,
                                },
                            ]
                        },
                    },
                ]
            }
        )
    )

    helper.summary(journal)

    output = capsys.readouterr().out
    assert output.startswith("Managed jobs refreshed and validated: 6\n\n")
    assert [line.split() for line in output.splitlines()[2:]] == [
        ["Owner", "Job", "instance", "Automation", "Outcome"],
        ["alice", "archive-repo-nas-abc123", "Schedule", "Left", "inactive"],
        ["alice", "archive-repo-usb-def456", "Schedule", "Left", "inactive"],
        ["alice", "documents", "Schedule", "Left", "inactive"],
        ["alice", "paused-scope456", "Trigger", "Left", "inactive"],
        ["alice", "wallet-scope123", "Trigger", "Resumed"],
        ["root", "documents", "Schedule", "Resumed"],
    ]


def test_summary_reports_no_installed_jobs(helper, tmp_path, capsys):
    journal = tmp_path / "journal.json"
    journal.write_text(
        json.dumps(
            {"accounts": [{"account": name, "plan": {"jobs": []}} for name in (None, "alice")]}
        )
    )

    helper.summary(journal)

    assert capsys.readouterr().out == "No existing managed jobs to refresh.\n"


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
            "ExecStart": "{ path=/usr/local/bin/vaultkeep ; argv[]=/usr/local/bin/vaultkeep "
            "--user --config /jobs/app.yaml trigger monitor ; ignore_errors=no ; }",
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
    monkeypatch.setattr(helper, "check_activity", lambda *args: None)
    layout = SimpleNamespace(
        timer=tmp_path / "vaultkeep@.timer", bin=Path("/usr/local/bin/vaultkeep")
    )
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
    assert message.count("Issue: ") == 3
    for name in ("system", "alice", "carol"):
        assert f"Issue: {tmp_path / f'{name}.yaml'}" in message
    assert "bob: permission denied" in message
    assert message.count("field: trigger.notifications") == 3
    assert message.count("field: destination.root") == 3


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


@pytest.mark.parametrize("status", [0, 7])
def test_account_operation_keeps_both_captured_streams_in_diagnostics(
    helper, monkeypatch, tmp_path, status
):
    transcript = tmp_path / "diagnostics.log"
    monkeypatch.setenv("VAULTKEEP_INSTALL_LOG", str(transcript))
    monkeypatch.setattr(
        helper.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, status, "account stdout\n", "account stderr\n"
        ),
    )
    if status:
        with pytest.raises(ValueError, match="account stderr"):
            helper.account_operation({"account": None}, "python", "validate", {})
    else:
        assert helper.account_operation({"account": None}, "python", "validate", {}) is None

    log = transcript.read_text()
    assert f"Exit code: {status}" in log
    assert "account stdout" in log and "account stderr" in log


def test_account_configuration_failure_propagates_without_prefixes(helper, monkeypatch, tmp_path):
    error = {
        "errors": [
            {
                "file": str(tmp_path / "app.yaml"),
                "issues": [
                    {"path": "application.user", "message": "User 'missing' does not exist."}
                ],
            }
        ]
    }
    monkeypatch.setattr(
        helper.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, json.dumps(error), "Traceback: getpwnam lookup failure"
        ),
    )
    with pytest.raises(ValueError) as caught:
        helper.account_operation({"account": None}, "python", "plan", {})
    assert str(caught.value) == (
        f"Issue: {tmp_path / 'app.yaml'}\n"
        "       User 'missing' does not exist (field: application.user)."
    )
    assert "getpwnam" not in str(caught.value)


@pytest.mark.parametrize(
    "changed,reason",
    [
        ({"LoadState": "masked"}, "Cannot resolve launcher definition: LoadState=masked"),
        ({"UnitFileState": "masked"}, "Unexpected systemd UnitFileState: 'masked'"),
    ],
)
def test_launcher_refusal_reports_account_unit_and_actual_states(
    helper, monkeypatch, tmp_path, changed, reason
):
    unit = "vaultkeep-trigger@Bisq_sd0-srv_backups-633490673caaf382.service"
    specs = [{"account": "bisquser", "command": ["user"]}]
    monkeypatch.setattr(helper, "account_spec", lambda *args: specs[0])
    monkeypatch.setattr(helper, "user_managers", lambda: [])
    monkeypatch.setattr(helper.pwd, "getpwall", lambda: [])
    monkeypatch.setattr(helper, "execute", lambda *a, **kw: f"{unit} loaded\n")
    state = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "UnitFileState": "enabled",
        **changed,
    }
    monkeypatch.setattr(helper, "properties", lambda *args: state)

    with pytest.raises(ValueError) as caught:
        helper.pause(tmp_path / "journal.json", None)

    message = str(caught.value)
    assert message.startswith(f"Issue: User 'bisquser', unit '{unit}'\n       ")
    assert reason in message
    assert len(message.splitlines()) == 2
    assert "ERROR:" not in message and "inconclusive" not in message
    assert not (tmp_path / "journal.json").exists()


def test_account_refusal_propagates_actionable_nonconfiguration_error(helper, monkeypatch):
    reason = "managed timer is missing: /etc/systemd/system/vaultkeep@app.timer.d/schedule.conf"
    monkeypatch.setattr(
        helper.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, json.dumps({"failure": reason}), "Traceback: ValueError: " + reason
        ),
    )
    with pytest.raises(ValueError) as caught:
        helper.account_operation({"account": None}, "python", "plan", {})
    assert str(caught.value) == f"Issue: User 'root', managed jobs\n       {reason}"
    assert "configuration" not in str(caught.value)


@pytest.fixture
def managed_monitor(helper, monkeypatch, tmp_path, valid_config, system_execution):
    """Disposable managed files with simulated service manager and execution scope."""
    import importlib

    from vaultkeep import maintenance
    from vaultkeep.trigger.config import load_config
    from vaultkeep.trigger.service import render_service, service_name

    support = importlib.import_module("install_support")
    home = tmp_path / "alice"
    root = home / ".config/systemd/user"
    root.mkdir(parents=True)
    jobs = home / ".config/vaultkeep/jobs"
    jobs.mkdir(parents=True)
    config = jobs / "app.yaml"
    valid_config["application"] = {
        "name": "bisq",
        "data_dir": str(home),
        "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/bisq"}]},
    }
    valid_config["trigger"] = {"enabled": True}
    config.write_text(json.dumps(valid_config))
    trigger = load_config(config)
    unit = service_name(trigger)
    fragment = root / unit
    fragment.write_text(render_service(trigger))
    raw_command = next(
        line[10:] for line in fragment.read_text().splitlines() if line.startswith("ExecStart=")
    )
    states = {
        unit: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "running",
            "FragmentPath": str(fragment),
            "DropInPaths": "",
            "UnitFileState": "enabled",
            "ExecStart": (
                f"{{ path=/usr/local/bin/vaultkeep ; argv[]={raw_command} ; ignore_errors=no ; }}"
            ),
            "Type": "simple",
            "Restart": "on-failure",
            "UMask": "0077",
            "MainPID": "99999999",
            "ControlPID": "0",
        }
    }
    entry = SimpleNamespace(pw_dir=str(home), pw_name="alice")
    original = helper.pwd.getpwnam
    monkeypatch.setattr(
        helper.pwd, "getpwnam", lambda name: entry if name == "alice" else original(name)
    )
    monkeypatch.setattr(helper.pwd, "getpwall", lambda: [entry])
    monkeypatch.setattr(helper, "user_managers", lambda: [("alice", 1000)])
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        if "show-environment" in argv:
            return ""
        if "list-jobs" in argv:
            return ""
        if "list-units" in argv or "list-unit-files" in argv:
            return (
                "\n".join(
                    f"{name} loaded {state['ActiveState']} {state['SubState']}"
                    for name, state in states.items()
                )
                if "--user" in argv
                else ""
            )
        if "stop" in argv or "start" in argv:
            state = states[argv[-1]]
            action = "start" if "start" in argv else "stop"
            state.update(
                ActiveState="active" if action == "start" else "inactive",
                SubState="running" if action == "start" else "dead",
            )
            return ""
        pytest.fail(f"unexpected command: {argv}")

    absent = {
        "LoadState": "not-found",
        "ActiveState": "inactive",
        "SubState": "dead",
        "FragmentPath": "",
        "DropInPaths": "",
        "ExecStart": "",
        "UnitFileState": "",
    }
    for module in (helper, support):
        monkeypatch.setattr(module, "execute", execute)
        monkeypatch.setattr(
            module, "properties", lambda command, name: dict(states.get(name, absent))
        )
    layout = SimpleNamespace(
        service=tmp_path / "system/vaultkeep@.service",
        timer=tmp_path / "system/vaultkeep@.timer",
        config=tmp_path / "etc",
        registry=tmp_path / "registry",
        bin=Path("/usr/local/bin/vaultkeep"),
        manifest=tmp_path / "manifest",
        var=tmp_path / "var",
    )
    # Validate with the candidate's real schema without changing account privileges.
    monkeypatch.setattr(
        helper,
        "account_operation",
        lambda spec, python, action, payload: maintenance.check_configurations(payload),
    )
    return SimpleNamespace(
        layout=layout,
        unit=unit,
        states=states,
        fragment=fragment,
        config=config,
        calls=calls,
        data=valid_config,
    )


@pytest.mark.parametrize(
    "active,sub",
    [
        ("active", "running"),
        ("activating", "auto-restart"),
        ("failed", "failed"),
        ("inactive", "dead"),
        ("activating", "start"),
        ("deactivating", "stop-sigterm"),
    ],
)
def test_launcher_identity_is_independent_of_runtime_state(helper, managed_monitor, active, sub):
    job = managed_monitor
    job.states[job.unit].update(ActiveState=active, SubState=sub)
    spec = helper.inventory(job.layout)["accounts"][1]
    assert spec["states"] == {job.unit: "enabled"}
    assert spec["active"] == ([] if active in ("inactive", "failed") else [job.unit])


@pytest.mark.parametrize("enabled", ["enabled", "enabled-runtime", "disabled"])
@pytest.mark.parametrize(
    "active,sub",
    [
        ("active", "running"),
        ("activating", "auto-restart"),
        ("failed", "failed"),
        ("inactive", "dead"),
    ],
)
def test_monitor_pause_resume_preserves_enablement_and_runtime_intent(
    helper, managed_monitor, tmp_path, enabled, active, sub
):
    job = managed_monitor
    job.states[job.unit].update(
        ActiveState=active,
        SubState=sub,
        UnitFileState=enabled,
        MainPID="99999999" if active == "active" else "0",
    )
    journal = tmp_path / "journal.json"
    helper.pause(journal, job.layout)
    helper.resume(journal)
    actions = [call for call in job.calls if "start" in call or "stop" in call]
    assert [call[-2] for call in actions] == (
        ["stop", "start"] if active in ("active", "activating") else []
    )
    assert job.states[job.unit]["UnitFileState"] == enabled


@pytest.mark.parametrize(
    "problem,reason",
    [
        ("marker", "ownership marker"),
        ("override", "local overrides"),
        ("executable", "ExecStart executable"),
        ("arguments", "ExecStart arguments"),
        ("location", "configured unit root"),
        ("identity", "unit identity"),
    ],
)
def test_foreign_or_malformed_launcher_is_rejected_without_mutation(
    helper, managed_monitor, tmp_path, problem, reason
):
    job = managed_monitor
    state = job.states[job.unit]
    if problem == "marker":
        job.fragment.write_text("# foreign\n")
    elif problem == "override":
        state["DropInPaths"] = "/override.conf"
    elif problem == "executable":
        state["ExecStart"] = state["ExecStart"].replace("/usr/local/bin/vaultkeep", "/bin/foreign")
    elif problem == "arguments":
        state["ExecStart"] = state["ExecStart"].replace("trigger monitor", "run")
    else:
        path = tmp_path / (job.unit if problem == "location" else "wrong.service")
        path.write_bytes(job.fragment.read_bytes())
        state["FragmentPath"] = str(path)
    before = job.fragment.read_bytes()
    with pytest.raises(ValueError, match=reason) as caught:
        helper.pause(tmp_path / "journal.json", job.layout)
    assert job.unit in str(caught.value)
    assert job.fragment.read_bytes() == before
    assert not any("stop" in call for call in job.calls)


def test_restarting_monitor_reports_exact_yaml_user_error_before_any_mutation(
    helper, managed_monitor, monkeypatch
):
    job = managed_monitor
    job.states[job.unit].update(ActiveState="activating", SubState="auto-restart", MainPID="0")
    job.data["application"]["user"] = "dutu"
    job.config.write_text(json.dumps(job.data))
    original = helper.pwd.getpwnam

    def account(name):
        if name == "dutu":
            raise KeyError(name)
        return original(name)

    monkeypatch.setattr(helper.pwd, "getpwnam", account)
    before = job.fragment.read_bytes(), job.config.read_bytes()
    with pytest.raises(ValueError) as caught:
        helper.check_configurations(job.layout, "/candidate/python")
    assert str(caught.value) == (
        f"Issue: {job.config}\n"
        "       Unsupported property for user execution; remove application.user. "
        "The application owner is the job's execution account (field: application.user)."
    )
    assert (job.fragment.read_bytes(), job.config.read_bytes()) == before
    assert not any("stop" in call for call in job.calls)


@pytest.mark.parametrize("problem", ["backup", "transition", "pid", "lock"])
def test_restarting_monitor_cannot_hide_protected_or_unknown_activity(
    helper, managed_monitor, tmp_path, problem
):
    import fcntl

    job = managed_monitor
    state = job.states[job.unit]
    state.update(ActiveState="activating", SubState="auto-restart", MainPID="0")
    if problem == "backup":
        job.states["vaultkeep@app.service"] = {
            **state,
            "ActiveState": "active",
            "SubState": "running",
        }
        reason = "protected backup/worker activity"
    elif problem == "transition":
        state["SubState"] = "start-post"
        reason = "Cannot establish safe monitor activity"
    elif problem == "pid":
        del state["MainPID"]
        reason = "MainPID is missing or invalid"
    else:
        job.layout.var.mkdir()
        locks = job.layout.var / "locks"
        locks.mkdir()
        reason = "held runtime/job lock"
    with (tmp_path / "backup.lock" if problem != "lock" else locks / "backup.lock").open(
        "w"
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match=reason):
            helper.pause(tmp_path / "journal.json", job.layout)
    assert not (tmp_path / "journal.json").exists()
    assert not any("stop" in call for call in job.calls)


def test_legacy_monitor_launcher_order_is_identified(helper, managed_monitor):
    job = managed_monitor
    state = job.states[job.unit]
    state["ExecStart"] = state["ExecStart"].replace("--user --config", "--user trigger --config")
    state["ExecStart"] = state["ExecStart"].replace(" trigger monitor ;", " monitor ;")
    assert helper.inventory(job.layout)["accounts"][1]["states"] == {job.unit: "enabled"}


def test_disabled_unloaded_monitor_is_discovered_from_unit_files(
    helper, managed_monitor, monkeypatch
):
    job = managed_monitor
    job.states[job.unit].update(ActiveState="inactive", SubState="dead", UnitFileState="disabled")
    execute = helper.execute
    monkeypatch.setattr(
        helper, "execute", lambda argv, **kw: "" if "list-units" in argv else execute(argv, **kw)
    )
    spec = helper.inventory(job.layout)["accounts"][1]
    assert spec["states"] == {job.unit: "disabled"} and spec["active"] == []
    helper.check_configurations(job.layout, "/candidate/python")
