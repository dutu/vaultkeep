"""Unit tests for native systemd schedule rendering."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from vaultkeep.config.models import JobConfig
from vaultkeep.errors import ConfigurationError, TimerError
from vaultkeep.schedule import (
    SERVICE_TEMPLATE,
    TIMER_TEMPLATE,
    TimerManager,
    TimerPaths,
    render_schedule,
)


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
    commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], *, check: bool = True) -> str:
        commands.append(command)
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
        f"{instance}: enabled=True installed=True" for instance in instances
    )
    assert commands == []
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
        f"{instance}: enabled=False installed=True" for instance in instances
    )
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

    assert manager.list() == (skipped, f"z_plain: enabled={enabled} installed=False")
    assert commands == []
    ordinary_validation = "valid z_plain: *-*-* 01:00:00" if enabled else "valid disabled z_plain"
    assert manager.validate_all() == (skipped, ordinary_validation)
    commands.clear()
    expected = (skipped, "create z_plain" if enabled else "disable z_plain")
    assert manager.sync(dry_run=True) == expected
    assert commands == []
    assert not manager.paths.units_root.exists()
    assert not manager.paths.registry_path.exists()

    assert manager.sync() == expected
    assert not manager._timer_drop_in_path("app").exists()
    assert manager._timer_drop_in_path("z_plain").is_file() == enabled
    assert manager.list() == (skipped, f"z_plain: enabled={enabled} installed={enabled}")


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

    for operation in (manager.list, manager.validate_all, manager.sync):
        with pytest.raises(ConfigurationError if damage == "invalid_parameter" else TimerError):
            operation()
    assert commands == []
    assert not manager._timer_drop_in_path("a_plain").exists()
    assert all(path.read_bytes() == content for path, content in before.items())


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
    assert not manager.paths.units_root.exists()


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

    assert manager.list() == (f"{instance}: enabled=True installed=False",)
    assert manager.sync(dry_run=True) == (f"create {instance}",)
    assert commands == []
    assert manager.sync() == (f"create {instance}",)
    prefix = ("systemctl", "--user") if manager.user_mode else ("systemctl",)
    assert (*prefix, "enable", "--now", f"vaultkeep@{instance}.timer") in commands
    assert manager.list() == (f"{instance}: enabled=True installed=True",)
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
    assert not manager.paths.units_root.exists()


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
    assert all(command[0] == "systemd-analyze" for command in commands)
