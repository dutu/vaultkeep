"""Unit tests for native systemd schedule rendering."""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from vaultkeep import schedule as schedule_module
from vaultkeep.config.models import JobConfig
from vaultkeep.errors import ConfigurationError, TimerError
from vaultkeep.schedule import (
    SERVICE_TEMPLATE,
    TIMER_TEMPLATE,
    TimerManager,
    TimerPaths,
    render_schedule,
)


def _install_templates(paths: TimerPaths) -> None:
    paths.units_root.mkdir(parents=True, exist_ok=True)
    (paths.units_root / "vaultkeep@.timer").write_text(TIMER_TEMPLATE)
    (paths.units_root / "vaultkeep@.service").write_text(SERVICE_TEMPLATE)


@pytest.mark.parametrize(
    ("interval", "day", "at", "window", "calendar", "delay", "fixed"),
    [
        ("hourly", None, "00:05", None, "*-*-* *:05:00", 0, False),
        ("daily", None, None, "01:00-05:00", "*-*-* 01:00:00", 14400, True),
        ("weekly", "sunday", "03:30", None, "Sun *-*-* 03:30:00", 0, False),
        ("monthly", 1, "03:30", None, "*-*-01 03:30:00", 0, False),
    ],
)
def test_schedule_renders_native_systemd_calendar(
    valid_config: dict[str, Any],
    interval: str,
    day: str | int | None,
    at: str | None,
    window: str | None,
    calendar: str,
    delay: int,
    fixed: bool,
) -> None:
    valid_config["schedule"] = {
        "enabled": True,
        "interval": interval,
        "day": day,
        "at": at,
        "window": window,
        "persistent": True,
    }

    rendered = render_schedule(JobConfig.model_validate(valid_config))

    assert rendered.on_calendar == calendar
    assert rendered.randomized_delay_seconds == delay
    assert rendered.fixed_random_delay is fixed
    assert "OnCalendar=\n" in rendered.drop_in()
    assert "Persistent=true" in rendered.drop_in()


def test_shared_templates_use_the_same_run_workflow() -> None:
    assert "vaultkeep --config /etc/vaultkeep/jobs/%i.yaml run" in SERVICE_TEMPLATE
    assert "Unit=vaultkeep@%i.service" in TIMER_TEMPLATE
    assert "OnCalendar" not in TIMER_TEMPLATE


def test_install_writes_owned_drop_in_and_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    valid_config["schedule"]["enabled"] = True
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    config_path = jobs / "app.yaml"
    YAML(typ="safe").dump(valid_config, config_path)
    paths = TimerPaths(jobs, tmp_path / "units", tmp_path / "state" / "instances.json")
    _install_templates(paths)
    manager = TimerManager(paths)
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(manager, "_run", lambda command, check=True: commands.append(command) or "")

    manager.install(config_path)

    drop_in = paths.units_root / "vaultkeep@app.timer.d" / "schedule.conf"
    assert "OnCalendar=*-*-* 01:00:00" in drop_in.read_text(encoding="utf-8")
    assert '"app"' in paths.registry_path.read_text(encoding="utf-8")
    assert ("systemctl", "enable", "--now", "vaultkeep@app.timer") in commands


def test_parameterized_install_derives_instance_and_writes_service_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    valid_config["schedule"]["enabled"] = True
    valid_config["destination"]["root"] = "/mnt/backups/{repo}"
    valid_config["destination"]["name_template"] = (
        "backup-{job}-{repo}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
    )
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    config_path = jobs / "app.yaml"
    YAML(typ="safe").dump(valid_config, config_path)
    paths = TimerPaths(jobs, tmp_path / "units", tmp_path / "state" / "instances.json")
    _install_templates(paths)
    manager = TimerManager(paths)
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(manager, "_run", lambda command, check=True: commands.append(command) or "")

    manager.install(config_path, runtime_params={"repo": "nas-a"})

    timer_drop_ins = tuple(paths.units_root.glob("vaultkeep@app--repo-nas-a--*.timer.d"))
    service_drop_ins = tuple(paths.units_root.glob("vaultkeep@app--repo-nas-a--*.service.d"))
    assert len(timer_drop_ins) == 1
    assert len(service_drop_ins) == 1
    override = (service_drop_ins[0] / "override.conf").read_text(encoding="utf-8")
    assert f"ConditionPathExists={config_path.resolve()}" in override
    assert f"--config {config_path.resolve()} --param repo=nas-a run" in override
    enabled_units = [
        command[-1] for command in commands if command[:3] == ("systemctl", "enable", "--now")
    ]
    assert len(enabled_units) == 1
    assert enabled_units[0].startswith("vaultkeep@app--repo-nas-a--")
    assert enabled_units[0].endswith(".timer")


def test_sync_dry_run_does_not_write_for_disabled_job(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    valid_config["schedule"] = {"enabled": False}
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    config_path = jobs / "app.yaml"
    YAML(typ="safe").dump(valid_config, config_path)
    paths = TimerPaths(jobs, tmp_path / "units", tmp_path / "state" / "instances.json")

    plan = TimerManager(paths).sync(dry_run=True)

    assert plan == ("disable app",)
    assert not paths.units_root.exists()
    assert not paths.registry_path.exists()


def test_sync_validates_all_jobs_before_changing_timers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    valid_config["schedule"]["enabled"] = True
    paths = TimerPaths(tmp_path / "jobs", tmp_path / "units", tmp_path / "instances.json")
    paths.jobs_root.mkdir()
    for job_id in ("a_valid", "z_invalid"):
        valid_config["job"]["id"] = job_id
        if job_id == "z_invalid":
            valid_config["schedule"] = {"enabled": True, "interval": "daily"}
        YAML(typ="safe").dump(valid_config, paths.jobs_root / f"{job_id}.yaml")
    manager = TimerManager(paths)
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(manager, "_run", lambda command, check=True: commands.append(command) or "")

    with pytest.raises(ConfigurationError):
        manager.sync()

    assert commands == []
    assert not paths.units_root.exists()
    assert not paths.registry_path.exists()


def test_validate_all_accepts_disabled_minimal_schedule(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    valid_config["schedule"] = {"enabled": False}
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    config_path = jobs / "app.yaml"
    YAML(typ="safe").dump(valid_config, config_path)
    paths = TimerPaths(jobs, tmp_path / "units", tmp_path / "state" / "instances.json")

    assert TimerManager(paths).validate_all() == ("valid disabled app",)


def test_user_timer_install_writes_full_user_units(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    valid_config["schedule"]["enabled"] = True
    jobs = tmp_path / "config" / "vaultkeep" / "jobs"
    jobs.mkdir(parents=True)
    config_path = jobs / "app.yaml"
    YAML(typ="safe").dump(valid_config, config_path)
    paths = TimerPaths(
        jobs,
        tmp_path / "config" / "systemd" / "user",
        tmp_path / "state" / "vaultkeep" / "instances.json",
    )
    manager = TimerManager(paths, user_mode=True)
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(manager, "_run", lambda command, check=True: commands.append(command) or "")

    manager.install(config_path)

    service = paths.units_root / "vaultkeep@app.service"
    timer = paths.units_root / "vaultkeep@app.timer"
    assert "--user --config" in service.read_text(encoding="utf-8")
    assert "User=root" not in service.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 01:00:00" in timer.read_text(encoding="utf-8")
    assert ("systemctl", "--user", "enable", "--now", "vaultkeep@app.timer") in commands


@pytest.fixture(params=[False, True], ids=["root", "user"])
def managed_timers(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TimerManager, list[tuple[str, ...]]]:
    jobs = tmp_path / ("user jobs" if request.param else "jobs")
    paths = TimerPaths(jobs, tmp_path / "units", tmp_path / "instances.json")
    paths.jobs_root.mkdir()
    manager = TimerManager(paths, user_mode=request.param)
    if not manager.user_mode:
        _install_templates(paths)
    commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], *, check: bool = True) -> str:
        commands.append(command)
        if "show" in command:
            instance = command[-1].removeprefix("vaultkeep@").removesuffix(".timer")
            installed = manager._timer_drop_in_path(instance).is_file()
            return (
                f"LoadState={'loaded' if installed else 'not-found'}\n"
                f"UnitFileState={'enabled' if installed else 'disabled'}\n"
                "ActiveState=inactive\nNextElapseUSecRealtime=\n"
            )
        return "enabled\n" if "is-enabled" in command else ""

    monkeypatch.setattr(manager, "_run", run)
    return manager, commands


def _parameterize_destination(config: dict[str, Any], field: str) -> None:
    if field in ("root", "both"):
        config["destination"]["root"] = "/mnt/backups/{repo}"
    if field in ("name_template", "both"):
        config["destination"]["name_template"] = (
            "backup-{job}-{repo}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
        )


@pytest.mark.parametrize("field", ["root", "name_template", "both"])
def test_bulk_schedules_reuse_each_installed_parameter_selection(
    managed_timers: tuple[TimerManager, list[tuple[str, ...]]],
    valid_config: dict[str, Any],
    field: str,
) -> None:
    manager, commands = managed_timers
    config_path = manager.paths.jobs_root / "app.yaml"
    valid_config["schedule"]["enabled"] = True
    _parameterize_destination(valid_config, field)
    YAML(typ="safe").dump(valid_config, config_path)
    for value in ("nas-a", "nas:b@c+"):
        manager.install(config_path, runtime_params={"repo": value})
    instances = sorted(json.loads(manager.paths.registry_path.read_text())["instances"])
    services = {
        instance: manager._service_drop_in_path(instance).read_bytes() for instance in instances
    }
    before = {
        path: path.read_bytes() for path in manager.paths.units_root.rglob("*") if path.is_file()
    }
    registry = manager.paths.registry_path.read_bytes()
    commands.clear()

    assert manager.list() == tuple(
        f"{instance}: enabled=True installed=True active=inactive next=n/a drift=False"
        for instance in instances
    )
    assert len(commands) == len(instances) and all("show" in command for command in commands)
    commands.clear()
    assert manager.validate_all() == tuple(
        f"valid {instance}: *-*-* 01:00:00" for instance in instances
    )
    assert commands == [("systemd-analyze", "calendar", "*-*-* 01:00:00")] * 2

    valid_config["schedule"]["window"] = "02:00-04:00"
    YAML(typ="safe").dump(valid_config, config_path)
    commands.clear()
    expected = tuple(f"update {instance}" for instance in instances)
    assert manager.sync(dry_run=True) == expected
    assert commands == []
    assert all(path.read_bytes() == content for path, content in before.items())
    assert manager.paths.registry_path.read_bytes() == registry

    assert manager.sync() == expected
    prefix = ("systemctl", "--user") if manager.user_mode else ("systemctl",)
    for instance in instances:
        assert (*prefix, "restart", f"vaultkeep@{instance}.timer") in commands
        assert "OnCalendar=*-*-* 02:00:00" in manager._timer_drop_in_path(instance).read_text()
        assert manager._service_drop_in_path(instance).read_bytes() == services[instance]
    assert not manager._timer_drop_in_path("app").exists()

    valid_config["schedule"] = {"enabled": False}
    YAML(typ="safe").dump(valid_config, config_path)
    commands.clear()
    assert manager.list() == tuple(
        f"{instance}: enabled=False installed=True active=inactive next=n/a drift=True"
        for instance in instances
    )
    assert all("show" in command for command in commands)
    commands.clear()
    assert manager.validate_all() == tuple(f"valid disabled {instance}" for instance in instances)
    assert commands == []
    expected = tuple(f"disable {instance}" for instance in instances)
    assert manager.sync(dry_run=True) == expected
    assert commands == []
    assert manager.sync() == expected
    assert commands == [
        command
        for instance in instances
        for command in (
            (*prefix, "is-enabled", f"vaultkeep@{instance}.timer"),
            (*prefix, "is-active", f"vaultkeep@{instance}.timer"),
        )
    ] + [
        command
        for instance in instances
        for command in (
            (*prefix, "disable", "--now", f"vaultkeep@{instance}.timer"),
            (*prefix, "clean", "--what=state", f"vaultkeep@{instance}.timer"),
        )
    ]


@pytest.mark.parametrize("field", ["root", "name_template", "both"])
@pytest.mark.parametrize("enabled", [False, True])
def test_bulk_schedules_skip_uninstalled_templates_and_continue(
    managed_timers: tuple[TimerManager, list[tuple[str, ...]]],
    valid_config: dict[str, Any],
    field: str,
    enabled: bool,
) -> None:
    manager, commands = managed_timers
    valid_config["schedule"]["enabled"] = enabled
    ordinary = deepcopy(valid_config)
    ordinary["job"]["id"] = "z_plain"
    YAML(typ="safe").dump(ordinary, manager.paths.jobs_root / "z_plain.yaml")
    _parameterize_destination(valid_config, field)
    YAML(typ="safe").dump(valid_config, manager.paths.jobs_root / "app.yaml")
    skipped = "skip app: no installed parameter selection; use schedule install with --param"

    assert manager.list() == (
        skipped,
        f"z_plain: enabled={enabled} installed=False active=inactive next=n/a drift={enabled}",
    )
    assert len(commands) == 1 and "show" in commands[0]
    commands.clear()
    ordinary_validation = "valid z_plain: *-*-* 01:00:00" if enabled else "valid disabled z_plain"
    assert manager.validate_all() == (skipped, ordinary_validation)
    commands.clear()
    expected = (skipped, "create z_plain" if enabled else "disable z_plain")
    assert manager.sync(dry_run=True) == expected
    assert commands == []
    assert not tuple(manager.paths.units_root.glob("vaultkeep@*.timer.d"))
    assert not tuple(manager.paths.units_root.glob("vaultkeep@*.service.d"))
    assert not manager.paths.registry_path.exists()

    assert manager.sync() == expected
    assert not manager._timer_drop_in_path("app").exists()
    assert manager._timer_drop_in_path("z_plain").is_file() == enabled
    assert manager.list() == (
        skipped,
        f"z_plain: enabled={enabled} installed={enabled} active=inactive next=n/a drift=False",
    )


@pytest.mark.parametrize(
    "damage",
    ["missing_service", "parameters", "invalid_parameter", "config_path", "command", "registry"],
)
def test_bulk_schedules_reject_damaged_installed_selections_before_mutating(
    managed_timers: tuple[TimerManager, list[tuple[str, ...]]],
    valid_config: dict[str, Any],
    damage: str,
) -> None:
    manager, commands = managed_timers
    valid_config["schedule"]["enabled"] = True
    valid_config["job"]["id"] = "a_plain"
    YAML(typ="safe").dump(valid_config, manager.paths.jobs_root / "a_plain.yaml")
    valid_config["job"]["id"] = "app"
    _parameterize_destination(valid_config, "both")
    config_path = manager.paths.jobs_root / "app.yaml"
    YAML(typ="safe").dump(valid_config, config_path)
    manager.install(config_path, runtime_params={"repo": "nas-a"})
    registry = json.loads(manager.paths.registry_path.read_text())
    instance = next(iter(registry["instances"]))
    service = manager._service_drop_in_path(instance)
    if damage == "missing_service":
        service.unlink()
    elif damage == "registry":
        registry["instances"][instance] = "/unexpected/timer"
        manager.paths.registry_path.write_text(json.dumps(registry))
    else:
        replacements = {
            "parameters": ("repo=nas-a", "repo=nas-b"),
            "invalid_parameter": ("repo=nas-a", "repo=../outside"),
            "config_path": (str(config_path), str(config_path.parent.parent / "outside.yaml")),
            "command": (" run", " prune"),
        }
        old, new = replacements[damage]
        service.write_text(service.read_text().replace(old, new))
    before = {
        path: path.read_bytes() for path in manager.paths.units_root.rglob("*") if path.is_file()
    }
    commands.clear()

    operations = [manager.list, manager.validate_all]
    # A missing root override restores the template's plain-instance mapping.
    if damage != "missing_service" or manager.user_mode:
        operations.append(manager.sync)
    for operation in operations:
        with pytest.raises(ConfigurationError if damage == "invalid_parameter" else TimerError):
            operation()
    assert commands == []
    assert not manager._timer_drop_in_path("a_plain").exists()
    assert all(path.read_bytes() == content for path, content in before.items())
    if damage == "missing_service" and not manager.user_mode:
        assert manager.sync(dry_run=True)[-1] == f"remove {instance}"
        assert commands == []


@pytest.mark.parametrize("instances", [[], {"../app": "/timer"}, {"app": None}])
def test_bulk_schedules_reject_invalid_ownership_registry(
    managed_timers: tuple[TimerManager, list[tuple[str, ...]]], instances: Any
) -> None:
    manager, commands = managed_timers
    manager.paths.registry_path.write_text(json.dumps({"instances": instances}))

    for operation in (manager.list, manager.validate_all, manager.sync):
        with pytest.raises(TimerError, match="Invalid timer ownership registry"):
            operation()
    assert commands == []
    assert not tuple(manager.paths.units_root.glob("vaultkeep@*.timer.d"))
    assert not tuple(manager.paths.units_root.glob("vaultkeep@*.service.d"))


def test_bulk_sync_recreates_missing_parameterized_timer(
    managed_timers: tuple[TimerManager, list[tuple[str, ...]]], valid_config: dict[str, Any]
) -> None:
    manager, commands = managed_timers
    config_path = manager.paths.jobs_root / "app.yaml"
    valid_config["schedule"]["enabled"] = True
    _parameterize_destination(valid_config, "both")
    YAML(typ="safe").dump(valid_config, config_path)
    manager.install(config_path, runtime_params={"repo": "nas-a"})
    instance = next(iter(json.loads(manager.paths.registry_path.read_text())["instances"]))
    manager._timer_drop_in_path(instance).unlink()
    commands.clear()

    assert manager.list() == (
        f"{instance}: enabled=True installed=False active=inactive next=n/a drift=True",
    )
    assert len(commands) == 1 and "show" in commands[0]
    commands.clear()
    assert manager.sync(dry_run=True) == (f"create {instance}",)
    assert commands == []
    assert manager.sync() == (f"create {instance}",)
    prefix = ("systemctl", "--user") if manager.user_mode else ("systemctl",)
    assert (*prefix, "enable", "--now", f"vaultkeep@{instance}.timer") in commands
    assert manager.list() == (
        f"{instance}: enabled=True installed=True active=inactive next=n/a drift=False",
    )
    assert not manager._timer_drop_in_path("app").exists()


@pytest.mark.parametrize("damage", ["schedule", "name_template", "filename"])
def test_skipping_uninstalled_templates_does_not_hide_other_configuration_errors(
    managed_timers: tuple[TimerManager, list[tuple[str, ...]]],
    valid_config: dict[str, Any],
    damage: str,
) -> None:
    manager, commands = managed_timers
    _parameterize_destination(valid_config, "root")
    if damage == "schedule":
        valid_config["schedule"] = {"enabled": True, "interval": "daily"}
    elif damage == "name_template":
        valid_config["destination"]["name_template"] = "{unfinished"
    else:
        valid_config["job"]["id"] = "mismatch"
    YAML(typ="safe").dump(valid_config, manager.paths.jobs_root / "app.yaml")

    for operation in (manager.list, manager.validate_all, manager.sync):
        with pytest.raises(TimerError if damage == "filename" else ConfigurationError):
            operation()
    assert commands == []
    assert not tuple(manager.paths.units_root.glob("vaultkeep@*.timer.d"))
    assert not tuple(manager.paths.units_root.glob("vaultkeep@*.service.d"))


@pytest.mark.parametrize("action", ["list", "validate", "sync"])
def test_bulk_cli_handles_installed_and_uninstalled_parameterized_jobs(
    managed_timers: tuple[TimerManager, list[tuple[str, ...]]],
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    from vaultkeep.cli import commands as cli_commands

    manager, commands = managed_timers
    valid_config["schedule"]["enabled"] = True
    _parameterize_destination(valid_config, "both")
    config_path = manager.paths.jobs_root / "app.yaml"
    YAML(typ="safe").dump(valid_config, config_path)
    manager.install(config_path, runtime_params={"repo": "nas-a"})
    instance = next(iter(json.loads(manager.paths.registry_path.read_text())["instances"]))
    valid_config["job"]["id"] = "z_template"
    YAML(typ="safe").dump(valid_config, manager.paths.jobs_root / "z_template.yaml")
    monkeypatch.setattr(cli_commands, "TimerManager", lambda **kwargs: manager)
    monkeypatch.setattr(manager, "require_environment", lambda: None)
    commands.clear()
    argv = [*(["--user"] if manager.user_mode else []), "schedules", action]
    if action == "sync":
        argv.append("--dry-run")

    assert cli_commands.main(argv) == 0

    output = capsys.readouterr()
    assert instance in output.out
    assert "skip z_template: no installed parameter selection" in output.out
    assert output.err == ""
    assert all(command[0] == "systemd-analyze" or "show" in command for command in commands)


@pytest.fixture
def syncing_timers(
    managed_timers: tuple[TimerManager, list[tuple[str, ...]]],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TimerManager, list[tuple[str, ...]], dict[str, tuple[bool, bool]]]:
    manager, commands = managed_timers
    states: dict[str, tuple[bool, bool]] = {}

    def run(command: tuple[str, ...], *, check: bool = True) -> str:
        commands.append(command)
        arguments = command[2:] if manager.user_mode else command[1:]
        if arguments == ("daemon-reload",):
            return ""
        operation, unit = arguments[0], arguments[-1]
        enabled, active = states.get(unit, (False, False))
        if operation == "is-enabled":
            return "enabled\n" if enabled else "disabled\n"
        if operation == "is-active":
            return "active\n" if active else "inactive\n"
        instance = unit.removeprefix("vaultkeep@").removesuffix(".timer")
        if (
            manager.user_mode
            and operation == "enable"
            and not manager._timer_drop_in_path(instance).is_file()
        ):
            raise TimerError(f"Unit file does not exist: {unit}")
        if operation == "enable":
            states[unit] = (True, active or "--now" in arguments)
        elif operation == "disable":
            states[unit] = (False, active and "--now" not in arguments)
        elif operation in ("restart", "stop"):
            states[unit] = (enabled, operation == "restart")
        return ""

    monkeypatch.setattr(manager, "_run", run)
    return manager, commands, states


def _save_sync_job(manager: TimerManager, config: dict[str, Any], job_id: str) -> Path:
    config = deepcopy(config)
    config["job"]["id"] = job_id
    path = manager.paths.jobs_root / f"{job_id}.yaml"
    YAML(typ="safe").dump(config, path)
    return path


def _sync_files(manager: TimerManager) -> dict[Path, tuple[bytes, int]]:
    paths = [path for path in manager.paths.units_root.rglob("*") if path.is_file()]
    if manager.paths.registry_path.exists():
        paths.append(manager.paths.registry_path)
    return {path: (path.read_bytes(), path.stat().st_mode & 0o777) for path in paths}


@pytest.mark.parametrize("action", ["install", "update"])
@pytest.mark.parametrize("failure", ["calendar", "verify"])
def test_single_schedule_validation_precedes_live_changes(
    managed_timers, valid_config, monkeypatch, action, failure
):
    manager, commands = managed_timers
    valid_config["schedule"]["enabled"] = True
    _parameterize_destination(valid_config, "both")
    path = _save_sync_job(manager, valid_config, "app")
    before = _sync_files(manager)
    run = manager._run
    staged = []

    def fail(command, *, check=True):
        if failure in command:
            if failure == "verify":
                timer, service = map(Path, command[-2:])
                staged.extend((timer, service))
                assert timer.is_file() and service.is_file()
                if manager.user_mode:
                    assert "OnCalendar=" in timer.read_text()
                    assert "--param repo=nas-a" in service.read_text()
                else:
                    assert timer.read_text() == TIMER_TEMPLATE
                    assert service.read_text() == SERVICE_TEMPLATE
                    drop_in = timer.parent / f"{timer.name}.d" / "schedule.conf"
                    override = service.parent / f"{service.name}.d" / "override.conf"
                    assert "OnCalendar=*-*-* 01:00:00" in drop_in.read_text()
                    assert "--param repo=nas-a" in override.read_text()
            raise TimerError("injected validation failure")
        return run(command, check=check)

    monkeypatch.setattr(manager, "_run", fail)
    with pytest.raises(TimerError, match="injected validation failure"):
        getattr(manager, action)(path, runtime_params={"repo": "nas-a"})
    assert _sync_files(manager) == before
    assert all(command[0] == "systemd-analyze" for command in commands)
    assert all(not path.exists() for path in staged)


@pytest.mark.parametrize("parameterized", [False, True])
def test_staged_units_pass_systemd_parser(managed_timers, valid_config, monkeypatch, parameterized):
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze is required to verify staged schedule units")
    manager, _ = managed_timers
    runtime = manager.paths.jobs_root.parent / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("VAULTKEEP_BIN_LINK", "/usr/bin/true")
    if not manager.user_mode:
        (manager.paths.units_root / "vaultkeep@.service").write_text(
            SERVICE_TEMPLATE.replace("/usr/local/bin/vaultkeep", "/usr/bin/true")
        )
    valid_config["schedule"]["enabled"] = True
    if parameterized:
        _parameterize_destination(valid_config, "both")
    path = _save_sync_job(manager, valid_config, "app")
    params = {"repo": "nas-a"} if parameterized else {}
    run = TimerManager._run.__get__(manager)
    commands = []

    def validate(command, *, check=True):
        commands.append(command)
        if command[0] == "systemd-analyze":
            return run(command, check=check)
        # All systemctl commands stay mocked: this test never starts a timer.
        return ""

    monkeypatch.setattr(manager, "_run", validate)
    try:
        manager.install(path, runtime_params=params)
    except TimerError as error:
        if "SO_PASSCRED failed: Operation not permitted" in str(error):
            pytest.skip("sandbox blocks systemd-analyze sockets; run outside that sandbox")
        raise
    assert any("calendar" in command for command in commands)
    assert any("verify" in command for command in commands)
    assert len(manager._registry()) == 1


@pytest.mark.parametrize("action", ["install", "update"])
@pytest.mark.parametrize("failure", ["reload", "lifecycle", "file", "registry", "interrupt"])
def test_single_schedule_restores_files_registry_and_states(
    syncing_timers, valid_config, monkeypatch, action, failure
):
    manager, commands, states = syncing_timers
    valid_config["schedule"]["enabled"] = True
    _parameterize_destination(valid_config, "both")
    path = _save_sync_job(manager, valid_config, "app")
    params = {"repo": "nas-a"}
    instance = manager._instance_id(path, "app", params)
    unit = manager._unit(instance)
    if action == "update":
        manager.install(path, runtime_params=params)
        manager._timer_drop_in_path(instance).chmod(0o640)
        valid_config["schedule"]["window"] = "02:00-04:00"
        _save_sync_job(manager, valid_config, "app")
    before = _sync_files(manager)
    previous_state = states.get(unit, (False, False))
    commands.clear()
    run = manager._run
    fired = False

    def fail(command, *, check=True):
        nonlocal fired
        output = run(command, check=check)
        operation = "enable" if action == "install" else "restart"
        target = "daemon-reload" in command if failure == "reload" else operation in command
        if failure in ("reload", "lifecycle", "interrupt") and target and not fired:
            fired = True
            if failure == "interrupt":
                raise KeyboardInterrupt
            raise TimerError("injected lifecycle failure")
        return output

    monkeypatch.setattr(manager, "_run", fail)
    write = schedule_module._atomic_write

    def fail_write(target, content, *, mode):
        nonlocal fired
        write(target, content, mode=mode)
        if not fired and (
            (failure == "registry" and target == manager.paths.registry_path)
            or (failure == "file" and target == manager._service_drop_in_path(instance))
        ):
            fired = True
            raise OSError("injected write failure")

    monkeypatch.setattr(schedule_module, "_atomic_write", fail_write)
    with pytest.raises(
        TimerError, match="interrupted; rolled back" if failure == "interrupt" else "rolled back"
    ):
        getattr(manager, action)(path, runtime_params=params)
    assert fired
    assert _sync_files(manager) == before
    assert states[unit] == previous_state
    if action == "install" and not manager.user_mode:
        assert not manager._timer_drop_in_path(instance).parent.exists()
        assert not manager._service_drop_in_path(instance).parent.exists()


@pytest.mark.parametrize(
    "enabled,active", [(False, False), (False, True), (True, False), (True, True)]
)
def test_update_preserves_enablement_and_restores_activity_on_failure(
    syncing_timers, valid_config, monkeypatch, enabled, active
):
    manager, _, states = syncing_timers
    valid_config["schedule"]["enabled"] = True
    path = _save_sync_job(manager, valid_config, "app")
    manager.install(path)
    unit = manager._unit("app")
    states[unit] = (enabled, active)
    manager.update(path)
    assert states[unit] == (enabled, True if enabled else active)

    states[unit] = (enabled, active)
    before = _sync_files(manager)
    write_registry = schedule_module._write_registry

    def fail_registry(path, instances):
        write_registry(path, instances)
        raise TimerError("injected registry failure")

    monkeypatch.setattr(schedule_module, "_write_registry", fail_registry)
    with pytest.raises(TimerError, match="rolled back"):
        manager.update(path)
    assert states[unit] == (enabled, active)
    assert _sync_files(manager) == before


def test_single_schedule_reports_incomplete_rollback(syncing_timers, valid_config, monkeypatch):
    manager, _, states = syncing_timers
    valid_config["schedule"]["enabled"] = True
    path = _save_sync_job(manager, valid_config, "app")
    manager.install(path)
    before = _sync_files(manager)
    run = manager._run

    def fail(command, *, check=True):
        if "restart" in command:
            raise TimerError("injected persistent restart failure")
        return run(command, check=check)

    monkeypatch.setattr(manager, "_run", fail)
    with pytest.raises(TimerError, match=r"rollback incomplete:.*persistent restart failure"):
        manager.update(path)
    assert _sync_files(manager) == before
    assert states[manager._unit("app")] == (True, False)


@pytest.mark.parametrize("damage", [None, "calendar", "timer", "service", "enablement", "disabled"])
def test_list_reports_live_state_next_trigger_and_drift(
    managed_timers, valid_config, monkeypatch, damage
):
    manager, commands = managed_timers
    valid_config["schedule"]["enabled"] = True
    _parameterize_destination(valid_config, "both")
    path = _save_sync_job(manager, valid_config, "app")
    manager.install(path, runtime_params={"repo": "nas-a"})
    instance = next(iter(manager._registry()))
    if damage == "calendar":
        valid_config["schedule"]["window"] = "02:00-04:00"
        _save_sync_job(manager, valid_config, "app")
    elif damage in ("timer", "service"):
        target = (
            manager._timer_drop_in_path(instance)
            if damage == "timer"
            else manager._service_drop_in_path(instance)
        )
        target.write_text(target.read_text() + "# modified\n")
    elif damage == "disabled":
        valid_config["schedule"] = {"enabled": False}
        _save_sync_job(manager, valid_config, "app")
    before = _sync_files(manager)
    commands.clear()

    def show(command, *, check=True):
        commands.append(command)
        assert "show" in command
        return (
            "LoadState=loaded\nActiveState=active\n"
            f"UnitFileState={'disabled' if damage == 'enablement' else 'enabled'}\n"
            "NextElapseUSecRealtime=Mon 2026-10-26 01:00:00 EET\n"
        )

    monkeypatch.setattr(manager, "_run", show)
    assert manager.list() == (
        f"{instance}: enabled={damage != 'disabled'} installed=True active=active "
        f"next=Mon 2026-10-26 01:00:00 EET drift={damage is not None}",
    )
    assert _sync_files(manager) == before
    assert len(commands) == 1
    assert ("--user" in commands[0]) == manager.user_mode


@pytest.mark.parametrize("response", ["", "LoadState=loaded\n"])
def test_list_fails_on_unavailable_or_incomplete_systemd_response(
    managed_timers, valid_config, monkeypatch, response
):
    manager, _ = managed_timers
    _save_sync_job(manager, valid_config, "app")
    before = _sync_files(manager)
    monkeypatch.setattr(manager, "_run", lambda *args, **kwargs: response)
    with pytest.raises(TimerError, match="incomplete systemd response"):
        manager.list()
    assert _sync_files(manager) == before


@pytest.mark.parametrize("parameterized", [False, True])
def test_sync_removes_deleted_jobs_and_preserves_unrelated_files(
    syncing_timers: tuple[TimerManager, list[tuple[str, ...]], dict[str, tuple[bool, bool]]],
    valid_config: dict[str, Any],
    parameterized: bool,
) -> None:
    manager, commands, states = syncing_timers
    valid_config["schedule"]["enabled"] = True
    kept = _save_sync_job(manager, valid_config, "kept")
    manager.install(kept)
    if parameterized:
        _parameterize_destination(valid_config, "both")
    deleted = _save_sync_job(manager, valid_config, "deleted")
    for params in ({"repo": "nas-a"}, {"repo": "nas-b"}) if parameterized else ({},):
        manager.install(deleted, runtime_params=params)
    instances = sorted(set(manager._registry()) - {"kept"})
    unrelated = manager.paths.units_root / "unrelated.timer"
    unrelated.write_text("operator timer\n")
    if not manager.user_mode:
        override = manager._timer_drop_in_path(instances[0]).parent / "operator.conf"
        override.write_text("[Timer]\nAccuracySec=1min\n")
    deleted.unlink()
    before = _sync_files(manager)
    commands.clear()
    expected = ("update kept", *(f"remove {instance}" for instance in instances))

    assert manager.sync(dry_run=True) == expected
    assert commands == []
    assert _sync_files(manager) == before
    assert manager.sync() == expected

    assert set(manager._registry()) == {"kept"}
    assert unrelated.read_text() == "operator timer\n"
    if not manager.user_mode:
        assert override.read_text() == "[Timer]\nAccuracySec=1min\n"
    for instance in instances:
        assert states[manager._unit(instance)] == (False, False)
        assert not manager._timer_drop_in_path(instance).exists()
        assert not manager._service_drop_in_path(instance).exists()
        operations = [command for command in commands if command[-1] == manager._unit(instance)]
        assert any("disable" in command and "--now" in command for command in operations)
        assert any("clean" in command and "--what=state" in command for command in operations)
    assert manager.sync(dry_run=True) == ("update kept",)


@pytest.mark.parametrize("action", ["update", "create", "disable", "remove"])
@pytest.mark.parametrize("failure", ["systemd", "registry", "interrupt"])
def test_sync_rolls_back_files_registry_and_timer_states(
    syncing_timers: tuple[TimerManager, list[tuple[str, ...]], dict[str, tuple[bool, bool]]],
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    failure: str,
) -> None:
    manager, commands, states = syncing_timers
    valid_config["schedule"]["enabled"] = True
    first = _save_sync_job(manager, valid_config, "a_first")
    manager.install(first)
    _parameterize_destination(valid_config, "both")
    second = _save_sync_job(manager, valid_config, "z_second")
    params = {"repo": "nas-a"}
    instance = manager._instance_id(second, "z_second", params)
    # Keep a saved selection for creates, with its timer file missing.
    manager.install(second, runtime_params=params)
    if action == "create":
        manager._timer_drop_in_path(instance).unlink()
        states[manager._unit(instance)] = (False, False)
    elif action == "disable":
        valid_config["schedule"] = {"enabled": False}
    elif action == "remove":
        second.unlink()
    if action != "remove":
        _save_sync_job(manager, valid_config, "z_second")
    valid_config["schedule"] = {"enabled": True, "interval": "daily", "at": "04:00"}
    plain = deepcopy(valid_config)
    plain["destination"]["root"] = "/mnt/backups/plain"
    plain["destination"]["name_template"] = "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
    _save_sync_job(manager, plain, "a_first")
    # Also verify restoration of a disabled but manually active timer.
    states[manager._unit("a_first")] = (False, True)
    manager._timer_drop_in_path("a_first").chmod(0o640)
    files_before = _sync_files(manager)
    states_before = dict(states)
    run = manager._run
    fired = False

    def fail(command: tuple[str, ...], *, check: bool = True) -> str:
        nonlocal fired
        output = run(command, check=check)
        operation = {"update": "restart", "create": "enable", "disable": "clean"}.get(action)
        target = (
            "daemon-reload" in command and not manager._timer_drop_in_path(instance).exists()
            if action == "remove"
            else operation in command and command[-1] == manager._unit(instance)
        )
        if failure in ("systemd", "interrupt") and not fired and target:
            fired = True
            if failure == "interrupt":
                raise KeyboardInterrupt
            raise TimerError("injected systemd failure")
        return output

    if failure in ("systemd", "interrupt"):
        monkeypatch.setattr(manager, "_run", fail)
    else:
        write_registry = schedule_module._write_registry

        def fail_registry(path: Path, instances: dict[str, str]) -> None:
            nonlocal fired
            write_registry(path, instances)
            fired = True
            raise OSError("injected registry failure after replacement")

        monkeypatch.setattr(schedule_module, "_write_registry", fail_registry)
    commands.clear()

    with pytest.raises(TimerError, match=r"(injected.*|interrupted); rolled back"):
        manager.sync()

    assert fired
    assert _sync_files(manager) == files_before
    assert states == states_before
    assert any("daemon-reload" in command for command in commands)


def test_sync_rolls_back_new_install_and_absent_registry_on_file_failure(
    syncing_timers: tuple[TimerManager, list[tuple[str, ...]], dict[str, tuple[bool, bool]]],
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, commands, states = syncing_timers
    before = _sync_files(manager)
    valid_config["schedule"]["enabled"] = True
    _save_sync_job(manager, valid_config, "a_first")
    _save_sync_job(manager, valid_config, "z_second")
    write = schedule_module._atomic_write

    def fail(path: Path, content: str | bytes, *, mode: int) -> None:
        if path == manager._timer_drop_in_path("z_second"):
            raise OSError("injected timer file failure")
        write(path, content, mode=mode)

    monkeypatch.setattr(schedule_module, "_atomic_write", fail)
    with pytest.raises(TimerError, match="injected timer file failure; rolled back"):
        manager.sync()

    assert _sync_files(manager) == before
    assert all(state == (False, False) for state in states.values())
    assert any("enable" in command and "--now" in command for command in commands)


@pytest.mark.parametrize("during_registry_write", [False, True])
def test_sync_rolls_back_new_install_on_interrupt(
    syncing_timers: tuple[TimerManager, list[tuple[str, ...]], dict[str, tuple[bool, bool]]],
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    during_registry_write: bool,
) -> None:
    manager, commands, states = syncing_timers
    before = _sync_files(manager)
    valid_config["schedule"]["enabled"] = True
    _save_sync_job(manager, valid_config, "app")
    if during_registry_write:
        write_registry = schedule_module._write_registry

        def interrupt_registry(path: Path, instances: dict[str, str]) -> None:
            write_registry(path, instances)
            raise KeyboardInterrupt

        monkeypatch.setattr(schedule_module, "_write_registry", interrupt_registry)
    else:
        run = manager._run

        def interrupt_enable(command: tuple[str, ...], *, check: bool = True) -> str:
            output = run(command, check=check)
            if "enable" in command and "--now" in command:
                raise KeyboardInterrupt
            return output

        monkeypatch.setattr(manager, "_run", interrupt_enable)

    with pytest.raises(TimerError, match="interrupted; rolled back"):
        manager.sync()

    assert _sync_files(manager) == before
    assert not manager.paths.registry_path.exists()
    assert states[manager._unit("app")] == (False, False)
    assert any("disable" in command for command in commands)
    assert any("daemon-reload" in command for command in commands)


def test_sync_reports_rollback_failures_and_continues_restoring(
    syncing_timers: tuple[TimerManager, list[tuple[str, ...]], dict[str, tuple[bool, bool]]],
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _, states = syncing_timers
    valid_config["schedule"]["enabled"] = True
    path = _save_sync_job(manager, valid_config, "app")
    manager.install(path)
    before = _sync_files(manager)
    valid_config["schedule"]["window"] = "02:00-04:00"
    _save_sync_job(manager, valid_config, "app")
    run = manager._run

    def fail(command: tuple[str, ...], *, check: bool = True) -> str:
        if "restart" in command:
            raise TimerError("injected persistent restart failure")
        return run(command, check=check)

    monkeypatch.setattr(manager, "_run", fail)
    with pytest.raises(
        TimerError, match=r"rollback incomplete:.*injected persistent restart failure"
    ):
        manager.sync()
    assert _sync_files(manager) == before
    assert states[manager._unit("app")] == (True, False)


def test_sync_does_not_operate_on_an_uninstalled_disabled_timer(
    syncing_timers: tuple[TimerManager, list[tuple[str, ...]], dict[str, tuple[bool, bool]]],
    valid_config: dict[str, Any],
) -> None:
    manager, commands, _ = syncing_timers
    valid_config["schedule"] = {"enabled": False}
    _save_sync_job(manager, valid_config, "app")

    assert manager.sync() == ("disable app",)
    assert all("is-enabled" in command or "is-active" in command for command in commands)
    assert not manager.paths.registry_path.exists()


@pytest.mark.parametrize("job_id", ["app", "app--repo-nas-a--0123456789abcdef"])
def test_sync_removes_the_final_job_and_is_idempotent(
    syncing_timers: tuple[TimerManager, list[tuple[str, ...]], dict[str, tuple[bool, bool]]],
    valid_config: dict[str, Any],
    job_id: str,
) -> None:
    manager, commands, states = syncing_timers
    valid_config["schedule"]["enabled"] = True
    path = _save_sync_job(manager, valid_config, job_id)
    manager.install(path)
    path.unlink()

    commands.clear()
    assert manager.sync(dry_run=True) == (f"remove {job_id}",)
    assert commands == []
    assert manager.sync() == (f"remove {job_id}",)
    assert manager._registry() == {}
    assert states[manager._unit(job_id)] == (False, False)
    assert not manager._timer_drop_in_path(job_id).exists()
    assert not manager._service_drop_in_path(job_id).exists()
    commands.clear()
    assert manager.sync() == ()
    assert commands == []


@pytest.mark.parametrize("clean_fails", [False, True])
def test_sync_cleans_deleted_timer_even_when_generated_timer_file_is_missing(
    syncing_timers: tuple[TimerManager, list[tuple[str, ...]], dict[str, tuple[bool, bool]]],
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    clean_fails: bool,
) -> None:
    manager, commands, states = syncing_timers
    valid_config["schedule"]["enabled"] = True
    path = _save_sync_job(manager, valid_config, "app")
    manager.install(path)
    path.unlink()
    manager._timer_drop_in_path("app").unlink()
    states[manager._unit("app")] = (False, False)
    before = _sync_files(manager)
    run = manager._run

    def fail_clean(command: tuple[str, ...], *, check: bool = True) -> str:
        output = run(command, check=check)
        if "clean" in command and clean_fails:
            raise TimerError("injected cleanup failure")
        return output

    monkeypatch.setattr(manager, "_run", fail_clean)
    commands.clear()
    assert manager.sync(dry_run=True) == ("remove app",)
    assert commands == []
    assert _sync_files(manager) == before
    if clean_fails:
        with pytest.raises(TimerError, match="injected cleanup failure; rolled back"):
            manager.sync()
        assert _sync_files(manager) == before
        assert "app" in manager._registry()
    else:
        assert manager.sync() == ("remove app",)
        assert manager._registry() == {}
        assert not manager._service_drop_in_path("app").exists()
    prefix = ("systemctl", "--user") if manager.user_mode else ("systemctl",)
    assert (*prefix, "disable", "--now", manager._unit("app")) in commands
    assert (*prefix, "clean", "--what=state", manager._unit("app")) in commands


@pytest.mark.parametrize("damage", ["missing_service", "outside_path", "wrong_job"])
def test_sync_handles_deleted_jobs_with_damaged_saved_selections(
    syncing_timers: tuple[TimerManager, list[tuple[str, ...]], dict[str, tuple[bool, bool]]],
    valid_config: dict[str, Any],
    damage: str,
) -> None:
    manager, commands, states = syncing_timers
    valid_config["schedule"]["enabled"] = True
    _save_sync_job(manager, valid_config, "a_plain")
    _parameterize_destination(valid_config, "both")
    path = _save_sync_job(manager, valid_config, "app")
    params = {"repo": "nas-a"}
    manager.install(path, runtime_params=params)
    instance = manager._instance_id(path, "app", params)
    service = manager._service_drop_in_path(instance)
    if damage == "missing_service":
        service.unlink()
    else:
        target = (
            path.parent.parent / "app.yaml"
            if damage == "outside_path"
            else path.with_name("wrong.yaml")
        )
        service.write_text(service.read_text().replace(str(path), str(target)))
    path.unlink()
    before = _sync_files(manager)
    states_before = dict(states)
    commands.clear()

    if damage == "missing_service" and not manager.user_mode:
        expected = ("create a_plain", f"remove {instance}")
        assert manager.sync(dry_run=True) == expected
        assert commands == []
        assert _sync_files(manager) == before
        assert manager.sync() == expected
        assert states[manager._unit(instance)] == (False, False)
        assert set(manager._registry()) == {"a_plain"}
        return

    for dry_run in (True, False):
        with pytest.raises(TimerError):
            manager.sync(dry_run=dry_run)
    assert commands == []
    assert _sync_files(manager) == before
    assert states == states_before


def test_sync_renders_the_whole_plan_before_mutating(
    syncing_timers: tuple[TimerManager, list[tuple[str, ...]], dict[str, tuple[bool, bool]]],
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, commands, _ = syncing_timers
    before = _sync_files(manager)
    valid_config["schedule"]["enabled"] = True
    _save_sync_job(manager, valid_config, "a_first")
    _save_sync_job(manager, valid_config, "z_second")
    render = manager._timer_content

    def fail(instance: str, rendered: schedule_module.RenderedSchedule) -> str:
        if instance == "z_second":
            raise TimerError("injected rendering failure")
        return render(instance, rendered)

    monkeypatch.setattr(manager, "_timer_content", fail)
    with pytest.raises(TimerError, match="injected rendering failure"):
        manager.sync()
    assert commands == []
    assert _sync_files(manager) == before


def test_sync_snapshots_the_whole_plan_before_mutating(
    syncing_timers: tuple[TimerManager, list[tuple[str, ...]], dict[str, tuple[bool, bool]]],
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, commands, _ = syncing_timers
    valid_config["schedule"]["enabled"] = True
    for job in ("a_first", "z_second"):
        manager.install(_save_sync_job(manager, valid_config, job))
    before = _sync_files(manager)
    read = Path.read_bytes

    def fail(path: Path) -> bytes:
        if path == manager._timer_drop_in_path("z_second"):
            raise OSError("injected snapshot failure")
        return read(path)

    commands.clear()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", fail)
        with pytest.raises(TimerError, match="Cannot snapshot schedules before sync"):
            manager.sync()
    assert commands == []
    assert _sync_files(manager) == before
