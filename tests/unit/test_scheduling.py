"""Unit tests for native systemd schedule rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from vaultkeep.config.models import JobConfig
from vaultkeep.scheduling import (
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
