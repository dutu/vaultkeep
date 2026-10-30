"""Recovery commands use strict identity and verified resources, independent of job schema."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.trigger.conftest import make_request
from vaultkeep.backup.execution import Execution
from vaultkeep.backup.paths import user_workflow_paths
from vaultkeep.cli import main
from vaultkeep.cli.status import collect_status
from vaultkeep.config.identity import load_job_identity
from vaultkeep.errors import TimerError
from vaultkeep.locking import JobLock, job_lock_path
from vaultkeep.schedule import TimerManager
from vaultkeep.trigger import service
from vaultkeep.trigger.config import load_config as load_trigger
from vaultkeep.trigger.errors import BlockedError
from vaultkeep.trigger.models import InstanceState, LaunchRecord, utc_now
from vaultkeep.trigger.paths import state_root
from vaultkeep.trigger.state import StateStore


@pytest.fixture
def resources(tmp_path, monkeypatch, valid_config):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr("vaultkeep.trigger.paths.account_home", lambda: tmp_path)
    monkeypatch.setattr(TimerManager, "require_environment", lambda _: None)
    path = tmp_path / "config/vaultkeep/jobs/app.yaml"
    path.parent.mkdir(parents=True)
    valid_config["application"] = {
        "name": "electrum",
        "data_dir": str(tmp_path / "wallet"),
        "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/electrum"}]},
    }
    valid_config["trigger"] = {"enabled": True}
    path.write_text(json.dumps(valid_config))
    config = load_trigger(path)
    monitor = service.service_name(config)
    manager = TimerManager(user_mode=True)
    timer = manager._unit("app")
    commands = []
    states = {}

    def systemctl(*args, **kwargs):
        if args[0].startswith("list-"):
            return ""
        commands.append(args)
        if args[0] == "disable":
            states[args[-1]].update(
                ActiveState="inactive", SubState="dead", UnitFileState="disabled"
            )
        return ""

    def inspect(unit, *, user_mode):
        assert user_mode is True
        return dict(states.get(unit, {"LoadState": "not-found"}))

    monkeypatch.setattr(service, "_systemctl", systemctl)
    monkeypatch.setattr(TimerManager, "_systemctl", lambda _, *a, **kw: systemctl(*a, **kw))
    monkeypatch.setattr(TimerManager, "_run", lambda *_a, **_kw: "")
    monkeypatch.setattr("vaultkeep.schedule.status.unit_properties", inspect)
    service.manage_service(config, "install")
    valid_config["trigger"]["enabled"] = False
    valid_config["schedule"]["enabled"] = True
    path.write_text(json.dumps(valid_config))
    manager.install(path)
    valid_config["trigger"]["enabled"] = True
    valid_config["schedule"]["enabled"] = False
    path.write_text(json.dumps(valid_config))
    for unit, fragment in (
        (monitor, service.units_root() / monitor),
        (timer, manager._timer_drop_in_path("app")),
    ):
        states[unit] = {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "running",
            "UnitFileState": "enabled",
            "FragmentPath": str(fragment),
            "DropInPaths": "",
            "NeedDaemonReload": "no",
            "ExecMainStatus": "0",
        }
    commands.clear()
    return SimpleNamespace(
        path=path,
        config=config,
        monitor=monitor,
        timer=timer,
        manager=manager,
        commands=commands,
        states=states,
        document=valid_config,
    )


def invalidate(resources):
    resources.document["future_settings"] = {"notifications": True}
    resources.path.write_text(json.dumps(resources.document))


def cli(resources, *command):
    return main(["--no-color", "--user", "--config", str(resources.path), *command])


@pytest.mark.parametrize("invalid", [False, True])
@pytest.mark.parametrize(
    "active,sub",
    [
        ("active", "running"),
        ("inactive", "dead"),
        ("failed", "failed"),
        ("activating", "auto-restart"),
    ],
)
def test_trigger_disable_preserves_config_and_targets_only_monitor(resources, invalid, active, sub):
    if invalid:
        invalidate(resources)
    resources.states[resources.monitor].update(ActiveState=active, SubState=sub)
    before = resources.path.read_bytes()
    assert cli(resources, "trigger", "disable") == 0
    assert resources.commands == [("disable", "--now", resources.monitor)]
    assert resources.path.read_bytes() == before
    assert resources.manager._timer_drop_in_path("app").exists()
    assert (service.units_root() / resources.monitor).exists()


def test_disable_is_idempotent_without_inventing_a_backup(resources):
    invalidate(resources)
    for _ in range(2):
        assert cli(resources, "trigger", "disable") == 0
        assert cli(resources, "schedule", "disable") == 0
    report = collect_status(resources.path, user_mode=True, runtime_params={})
    assert report["backup"]["activity"] == "IDLE"
    assert report["backup"]["previous"] is None
    assert report["trigger"]["enabled"] is False
    assert report["schedule"]["enabled"] is False


@pytest.mark.parametrize("damage", ["unknown", "missing", "obsolete"])
def test_schedule_disable_ignores_unrelated_schema_errors(resources, damage):
    if damage == "unknown":
        invalidate(resources)
    else:
        resources.document.pop("destination")
        if damage == "obsolete":
            resources.document["config_version"] = 100
            resources.document["archive"] = "old-format"
        resources.path.write_text(json.dumps(resources.document))
    assert cli(resources, "schedule", "disable") == 0
    assert resources.commands == [
        ("disable", "--now", resources.timer),
        ("clean", "--what=state", resources.timer),
    ]
    assert resources.states[resources.monitor]["ActiveState"] == "active"


@pytest.mark.parametrize("json_output", [False, True])
def test_status_reports_invalid_configuration_and_restart_with_backup_history(
    resources, capsys, json_output
):
    identity = load_job_identity(resources.path, runtime_params={})
    with Execution(user_workflow_paths(), "app", identity.scope) as execution:
        execution.complete("created")
    invalidate(resources)
    resources.states[resources.monitor].update(
        ActiveState="activating", SubState="auto-restart", ExecMainStatus="2"
    )
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in resources.path.parents[3].rglob("*")
        if p.is_file()
    }
    args = ["status", "--json"] if json_output else ["status"]
    assert cli(resources, *args) == 3
    output = capsys.readouterr().out
    if json_output:
        data = json.loads(output)
        assert data["job_id"] == "app"
        assert data["configuration"] == {
            "status": "INVALID",
            "errors": [{"path": "future_settings", "message": "Unknown property."}],
        }
        assert data["trigger"]["monitor"] == "RESTARTING"
        assert data["trigger"]["last_exit"] == 2
        assert data["trigger"]["enabled"] is True
        assert data["trigger"]["application"] == "UNKNOWN"
        assert data["schedule"]["health"] == "RUNNING"
        assert data["backup"]["previous"]["result"] == "SUCCESS"
    else:
        assert "INVALID" in output and "future_settings: Unknown property." in output
        assert "RESTARTING" in output and "Last exit" in output and "SUCCESS" in output
    assert resources.commands == []
    assert {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in resources.path.parents[3].rglob("*")
        if p.is_file()
    } == before


@pytest.mark.parametrize(
    "contents",
    [
        "job: [",
        "job: {}",
        "{}",
        "job: {id: 123}",
        "job: {id: ../other}",
        "job: {id: app, id: other}",
    ],
)
@pytest.mark.parametrize("command", [("trigger", "disable"), ("schedule", "disable"), ("status",)])
def test_unresolvable_yaml_identity_uses_only_verified_trigger_metadata(
    resources, contents, command
):
    resources.path.write_text(contents)
    result = cli(resources, *command)
    if command == ("trigger", "disable"):
        assert result == 0
        assert resources.commands == [("disable", "--now", resources.monitor)]
    else:
        assert result in (2, 3)
        assert resources.commands == []


@pytest.mark.parametrize("unreadable", [False, True])
@pytest.mark.parametrize("command", [("trigger", "disable"), ("schedule", "disable"), ("status",)])
def test_missing_or_unreadable_config_preserves_verified_trigger_recovery(
    resources, monkeypatch, unreadable, command
):
    if unreadable:
        original = Path.open

        def open_config(path, *a, **kw):
            if path == resources.path:
                raise PermissionError("configuration permission denied")
            return original(path, *a, **kw)

        monkeypatch.setattr(Path, "open", open_config)
    else:
        resources.path.unlink()
    result = cli(resources, *command)
    if command == ("trigger", "disable"):
        assert result == 0
        assert resources.commands == [("disable", "--now", resources.monitor)]
    else:
        assert result in (2, 3)
        assert resources.commands == []


@pytest.mark.parametrize(
    "damage",
    [
        "marker",
        "command",
        "params",
        "symlink",
        "overrides",
        "fragment",
        "dropins",
        "reload",
        "owner",
        "directives",
    ],
)
def test_unrelated_or_unverified_monitor_cannot_be_disabled(resources, damage, monkeypatch):
    invalidate(resources)
    path = service.units_root() / resources.monitor
    if damage == "marker":
        path.write_text("# unrelated service\n")
    elif damage == "command":
        path.write_text(path.read_text().replace(str(resources.path), "/other/app.yaml"))
    elif damage == "params":
        path.write_text(
            path.read_text().replace(" trigger monitor", " --param repo=other trigger monitor")
        )
    elif damage == "symlink":
        target = path.with_suffix(".unrelated")
        path.rename(target)
        path.symlink_to(target)
    elif damage == "overrides":
        path.with_name(path.name + ".d").mkdir()
    elif damage == "directives":
        path.write_text(path.read_text().replace("Type=simple", "Type=simple\nUser=other"))
    elif damage == "owner":
        original = Path.lstat

        def lstat(candidate, *a, **kw):
            info = original(candidate, *a, **kw)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=-1) if candidate == path else info

        monkeypatch.setattr(Path, "lstat", lstat)
    else:
        key, value = {
            "fragment": ("FragmentPath", "/other/unit"),
            "dropins": ("DropInPaths", "/override"),
            "reload": ("NeedDaemonReload", "yes"),
        }[damage]
        resources.states[resources.monitor][key] = value
    assert cli(resources, "trigger", "disable") == 2
    assert resources.commands == []


@pytest.mark.parametrize(
    "damage", ["registry", "command", "symlink", "fragment", "dropins", "timer"]
)
def test_unrelated_or_unverified_timer_cannot_be_disabled(resources, damage):
    invalidate(resources)
    manager = resources.manager
    if damage == "registry":
        manager.paths.registry_path.write_text('{"version":1,"instances":{}}')
    elif damage == "command":
        path = manager._service_drop_in_path("app")
        path.write_text(path.read_text().replace(str(resources.path), "/other/app.yaml"))
    elif damage == "symlink":
        path = manager._timer_drop_in_path("app")
        target = path.with_suffix(".unrelated")
        path.rename(target)
        path.symlink_to(target)
    elif damage == "timer":
        path = manager._timer_drop_in_path("app")
        path.write_text(
            path.read_text().replace("Unit=vaultkeep@app.service", "Unit=other.service")
        )
    else:
        resources.states[resources.timer][
            "FragmentPath" if damage == "fragment" else "DropInPaths"
        ] = "/other/unit"
    assert cli(resources, "schedule", "disable") == 14
    assert resources.commands == []


@pytest.mark.parametrize("state", ["active", "orphan", "blocked", "corrupt", "blocking"])
def test_monitor_stop_is_blocked_by_unreconciled_or_unsafe_state(resources, state):
    invalidate(resources)
    store = StateStore(state_root(resources.config.scope))
    if state == "blocked":
        store.write_blocked("app", "unsafe state")
    elif state == "corrupt":
        store.ensure()
        store.instance_path("app").write_text("broken")
    elif state == "blocking":
        store.write_instance(InstanceState(instance_id="app", blocking_attempt_id="unreconciled"))
    else:
        request = make_request(resources.config.instances[0])
        store.write_request(request)
        if state == "active":
            store.write_instance(
                InstanceState(instance_id="app", active_attempt_id=request.attempt_id)
            )
        else:
            store.write_launch(LaunchRecord(request.attempt_id, "worker.service", utc_now()))
    assert cli(resources, "trigger", "disable") in (2, 3)
    assert resources.commands == []


def test_active_backup_lock_prevents_monitor_stop(resources):
    invalidate(resources)
    lock = JobLock(
        job_lock_path(
            root=user_workflow_paths().lock_root, job_id="app", identity_hash=resources.config.scope
        )
    )
    lock.acquire()
    try:
        assert cli(resources, "trigger", "disable") == 3
        assert resources.commands == []
    finally:
        lock.release()


@pytest.mark.parametrize(
    "command",
    [
        ("run",),
        ("trigger", "install"),
        ("trigger", "update"),
        ("trigger", "monitor"),
        ("schedule", "install"),
        ("schedule", "update"),
    ],
)
def test_execution_and_enable_operations_keep_full_schema_validation(resources, command):
    invalidate(resources)
    assert cli(resources, *command) in (2, 3)
    assert resources.commands == []


def test_failed_disable_is_reported(resources, monkeypatch):
    invalidate(resources)

    def fail(*a):
        raise BlockedError("permission denied by user manager")

    monkeypatch.setattr(service, "_systemctl", fail)
    assert cli(resources, "trigger", "disable") == 3

    def fail_timer(*a, **kw):
        assert kw.get("check", True)
        raise TimerError("permission denied by system manager")

    monkeypatch.setattr(TimerManager, "_systemctl", fail_timer)
    assert cli(resources, "schedule", "disable") == 14


def test_status_retains_report_when_unit_ownership_is_unknown(resources, capsys):
    invalidate(resources)
    resources.states[resources.monitor]["FragmentPath"] = "/other/monitor.service"
    resources.states[resources.timer]["DropInPaths"] = "/local/override.conf"
    assert cli(resources, "status", "--json") == 15
    data = json.loads(capsys.readouterr().out)
    assert data["configuration"]["status"] == "INVALID"
    assert data["trigger"]["monitor"] == "UNKNOWN"
    assert data["trigger"]["enabled"] is None
    assert data["schedule"]["health"] == "UNKNOWN"
    assert data["schedule"]["enabled"] is None
    assert resources.commands == []


def test_trigger_defaults_and_explicit_parameters_reuse_original_identity(resources):
    resources.document["trigger"]["params"] = {"repo": "one"}
    resources.document["destination"]["root"] += "/{repo}"
    resources.path.write_text(json.dumps(resources.document))
    config = load_trigger(resources.path, runtime_params={"repo": "two"})
    unit = service.service_name(config)
    service.manage_service(config, "update")
    assert unit == resources.monitor
    resources.states[unit] = dict(
        resources.states[resources.monitor], FragmentPath=str(service.units_root() / unit)
    )
    resources.commands.clear()
    invalidate(resources)
    assert (
        main(
            ["--user", "--config", str(resources.path), "--param", "repo=two", "trigger", "disable"]
        )
        == 0
    )
    assert resources.commands == [("disable", "--now", unit)]
    assert resources.states[resources.monitor]["ActiveState"] == "inactive"


@pytest.mark.parametrize(
    "document",
    [
        {"job": {"id": "app"}},
        {
            "job": {"id": "app", "future_label": "Wallet"},
            "config_version": 99,
            "trigger": {"future_matcher": True},
        },
    ],
)
def test_trigger_disable_only_requires_recognizable_identity(resources, document):
    resources.path.write_text(json.dumps(document))
    assert cli(resources, "trigger", "disable") == 0
    assert resources.commands == [("disable", "--now", resources.monitor)]


@pytest.mark.parametrize(
    "params", [None, [], {"repo": 123}, {"repo": "has spaces"}, {"timestamp": "reserved"}]
)
def test_invalid_current_trigger_parameters_do_not_replace_installed_identity(resources, params):
    resources.document["trigger"]["params"] = params
    resources.path.write_text(json.dumps(resources.document))
    assert cli(resources, "trigger", "disable") == 0
    assert resources.commands == [("disable", "--now", resources.monitor)]


def test_bad_trigger_settings_do_not_hide_other_status_sections(resources, capsys):
    resources.document["trigger"] = "future-format"
    resources.path.write_text(json.dumps(resources.document))
    assert cli(resources, "status", "--json") == 3
    result = json.loads(capsys.readouterr().out)
    assert result["configuration"]["status"] == "INVALID"
    assert result["trigger"]["monitor"] == "UNKNOWN"
    assert result["backup"]["activity"] == "IDLE"
    assert result["schedule"]["enabled"] is True
    assert resources.commands == []


@pytest.mark.parametrize("user_mode", [False, True])
@pytest.mark.parametrize("parameters", [{}, {"repo": "one"}])
def test_schedule_recovery_uses_existing_scope_and_parameter_unit_identity(
    tmp_path, valid_config, monkeypatch, user_mode, parameters, system_execution
):
    from vaultkeep.config import JobConfig
    from vaultkeep.schedule import TIMER_TEMPLATE, TimerPaths, render_schedule

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    path = jobs / "app.yaml"
    path.write_text(json.dumps({"job": {"id": "app"}, "future_property": True}))
    manager = TimerManager(
        TimerPaths(jobs, tmp_path / "units", tmp_path / "registry.json"), user_mode=user_mode
    )
    instance = manager._instance_id(path, "app", parameters)
    timer = manager._timer_drop_in_path(instance)
    timer.parent.mkdir(parents=True)
    valid_config["schedule"]["enabled"] = True
    timer.write_text(
        manager._timer_content(instance, render_schedule(JobConfig.model_validate(valid_config)))
    )
    fragment = timer if user_mode else manager.paths.units_root / "vaultkeep@.timer"
    if not user_mode:
        fragment.write_text(TIMER_TEMPLATE)
    owned = {timer, fragment}
    if user_mode or parameters:
        service_file = manager._service_drop_in_path(instance)
        service_file.parent.mkdir(parents=True, exist_ok=True)
        service_file.write_text(manager._service_content(path, instance, parameters))
        owned.add(service_file)
    manager.paths.registry_path.write_text(json.dumps({"instances": {instance: str(timer)}}))
    original = Path.lstat

    def stat_owned(path, *a, **kw):
        info = original(path, *a, **kw)
        return (
            SimpleNamespace(st_mode=info.st_mode, st_uid=0)
            if not user_mode and path in owned
            else info
        )

    monkeypatch.setattr(Path, "lstat", stat_owned)
    calls = []
    monkeypatch.setattr(manager, "_systemctl", lambda *a, **kw: calls.append(a) or "")

    def inspect(unit, **kw):
        assert kw["user_mode"] == user_mode
        assert unit == manager._unit(instance)
        return {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "SubState": "dead",
            "UnitFileState": "disabled",
            "FragmentPath": str(fragment),
            "DropInPaths": "" if user_mode else str(timer),
            "NeedDaemonReload": "no",
        }

    monkeypatch.setattr("vaultkeep.schedule.status.unit_properties", inspect)
    manager.disable(path, runtime_params=parameters)
    assert calls == [
        ("disable", "--now", manager._unit(instance)),
        ("clean", "--what=state", manager._unit(instance)),
    ]


def test_uninstalled_schedule_disable_is_idempotent(resources):
    resources.manager._timer_drop_in_path("app").unlink()
    resources.manager._service_drop_in_path("app").unlink()
    resources.manager.paths.registry_path.write_text('{"instances": {}}')
    del resources.states[resources.timer]
    invalidate(resources)
    assert cli(resources, "schedule", "disable") == 0
    assert resources.commands == []


def test_schedule_path_and_job_id_conflict_is_never_ignored(resources):
    resources.document["job"]["id"] = "other"
    invalidate(resources)
    assert cli(resources, "schedule", "disable") == 14
    assert cli(resources, "trigger", "disable") == 0
    assert resources.commands == [("disable", "--now", resources.monitor)]


def test_invalid_yaml_settings_do_not_hide_orphan_trigger_recovery(resources):
    invalidate(resources)
    (service.units_root() / resources.monitor).unlink()
    del resources.states[resources.monitor]
    store = StateStore(state_root(resources.config.scope))
    store.write_blocked("app", "retained recovery evidence without a unit")
    report = collect_status(resources.path, user_mode=True, runtime_params={})
    assert report["trigger"]["monitor"] == "NOT INSTALLED"
    assert report["trigger"]["dispatch"] == "BLOCKED"
    assert "retained recovery evidence" in report["trigger"]["diagnostics"][0]
    assert resources.commands == []
