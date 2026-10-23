"""Schedule health is independent of backup outcomes and never manages timers."""

from __future__ import annotations

import json
import subprocess

import pytest

from vaultkeep.cli.status import collect_status
from vaultkeep.config import JobConfig
from vaultkeep.schedule import TimerManager
from vaultkeep.schedule.status import schedule_status


@pytest.mark.parametrize(
    "load,active,enabled,expected",
    [
        ("loaded", "active", "enabled", "RUNNING"),
        ("not-found", "inactive", "", "NOT INSTALLED"),
        ("loaded", "failed", "enabled", "FAILED"),
        ("loaded", "inactive", "disabled", "STOPPED"),
        ("loaded", "inactive", "masked", "BLOCKED"),
    ],
)
@pytest.mark.parametrize("user_mode", [False, True])
def test_timer_health_and_next_execution(
    valid_config, tmp_path, monkeypatch, load, active, enabled, expected, user_mode
):
    valid_config["schedule"]["enabled"] = True

    def query(argv, **kwargs):
        assert "show" in argv and "status" not in argv
        assert ("--user" in argv) == user_mode
        return subprocess.CompletedProcess(
            argv,
            0,
            (
                f"LoadState={load}\nActiveState={active}\nSubState=waiting\n"
                f"UnitFileState={enabled}\n"
                "NextElapseUSecRealtime=Fri 2026-10-23 22:00:00 UTC\nResult=success\n"
            ),
            "",
        )

    monkeypatch.setattr("vaultkeep.schedule.status.subprocess.run", query)
    result = schedule_status(
        tmp_path / "app.yaml",
        JobConfig.model_validate(valid_config),
        user_mode=user_mode,
        runtime_params={"repo": "nas-a"},
    )
    assert result["health"] == expected
    assert result["next_execution"] == "Fri 2026-10-23 22:00:00 UTC"
    assert "repo-nas-a" in result["timer"]


def test_schedule_reporting_does_not_require_managed_config_path(
    valid_config, tmp_path, monkeypatch
):
    path = tmp_path / "unmanaged.yaml"
    valid_config["schedule"]["enabled"] = True
    path.write_text(json.dumps(valid_config))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(
        TimerManager, "require_environment", lambda *_: pytest.fail("no runtime preflight")
    )
    monkeypatch.setattr(
        "vaultkeep.schedule.status.unit_properties",
        lambda *_args, **_kwargs: {"LoadState": "not-found"},
    )
    result = collect_status(path, user_mode=True, runtime_params={})
    assert result["schedule"]["health"] == "NOT INSTALLED"
    assert result["backup"]["activity"] == "IDLE"
    assert "trigger" not in result


def test_unavailable_systemd_is_unknown_without_mutation(valid_config, tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("systemctl", 15)

    monkeypatch.setattr("vaultkeep.schedule.status.subprocess.run", fail)
    before = list(tmp_path.rglob("*"))
    result = schedule_status(
        tmp_path / "app.yaml",
        JobConfig.model_validate(valid_config),
        user_mode=True,
        runtime_params={},
    )
    assert result["health"] == "UNKNOWN" and result["read_error"]
    assert list(tmp_path.rglob("*")) == before
