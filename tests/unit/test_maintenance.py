"""Installed-job refresh and deployment-lock regressions, without live managers."""

from __future__ import annotations

import base64
import copy
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vaultkeep import maintenance
from vaultkeep.cli import main
from vaultkeep.errors import LockError
from vaultkeep.locking import deployment_lock
from vaultkeep.scheduling import TimerManager, TimerPaths
from vaultkeep.trigger.config import load_config
from vaultkeep.trigger.service import render_service, service_name


def document(tmp_path: Path, *, user: bool) -> dict:
    return {
        "user": user,
        "unit_roots": [str(tmp_path / "units")],
        "paths": {
            "jobs_root": str(tmp_path / "jobs"),
            "units_root": str(tmp_path / "units"),
            "registry_path": str(tmp_path / "state/instances.json"),
        },
    }


def scheduled_job(tmp_path, valid_config, monkeypatch, *, user, params=None):
    spec = document(tmp_path, user=user)
    paths = TimerPaths(**{key: Path(value) for key, value in spec["paths"].items()})
    paths.jobs_root.mkdir()
    config = paths.jobs_root / "app.yaml"
    valid_config["schedule"]["enabled"] = True
    if params:
        valid_config["destination"]["root"] = "/mnt/backups/{repo}"
        valid_config["destination"]["name_template"] = (
            "backup-{job}-{repo}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
        )
    config.write_text(json.dumps(valid_config))
    manager = TimerManager(paths, user_mode=user)
    monkeypatch.setattr(manager, "_systemctl", lambda *args, **kwargs: "")
    manager.install(config, runtime_params=params)
    monkeypatch.setattr(
        "vaultkeep.scheduling.subprocess.run",
        lambda *args, **kwargs: pytest.fail("refresh must not invoke systemctl"),
    )
    return spec, paths, config


@pytest.mark.parametrize("user", [False, True])
@pytest.mark.parametrize("params", [None, {"repo": "nas-a"}])
def test_refresh_existing_schedules_and_rollback_preserve_selection_and_files(
    tmp_path, valid_config, monkeypatch, user, params
):
    spec, paths, config = scheduled_job(
        tmp_path, valid_config, monkeypatch, user=user, params=params
    )
    originals = {
        path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
        for path in paths.units_root.rglob("*")
        if path.is_file()
    }
    registry = paths.registry_path.read_bytes()
    valid_config["schedule"]["window"] = "02:00-04:00"
    config.write_text(json.dumps(valid_config))
    plan = maintenance.plan(spec)
    assert paths.registry_path.read_bytes() == registry
    assert all(path.read_bytes() == before[0] for path, before in originals.items())
    assert len(plan["jobs"]) == 1
    assert plan["jobs"][0]["params"] == (params or {})
    assert plan["jobs"][0]["config"] == str(config)

    maintenance.apply(plan)
    timer = next(path for path in originals if ".timer" in str(path))
    assert "02:00:00" in timer.read_text()
    assert paths.registry_path.read_bytes() == registry
    maintenance.apply(plan, restore=True)
    for path, before in originals.items():
        assert (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) == before


def monitor_job(tmp_path, valid_config):
    spec = document(tmp_path, user=True)
    root = Path(spec["unit_roots"][0])
    root.mkdir()
    valid_config["trigger"] = {
        "enabled": True,
        "app": "electrum",
        "data_dir": "/home/user/.electrum",
        "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/electrum"}]},
    }
    config = tmp_path / "directory with spaces/app.yaml"
    config.parent.mkdir()
    config.write_text(json.dumps(valid_config))
    trigger = load_config(config)
    path = root / service_name(trigger)
    new = render_service(trigger)
    old = new.replace("vaultkeep --user --config", "vaultkeep --user trigger --config")
    old = old.replace(" trigger monitor\n", " monitor\n")
    path.write_text(old)
    path.chmod(0o600)
    return spec, path, new


def test_refresh_old_monitor_command_without_activating_or_changing_permissions(
    tmp_path, valid_config, monkeypatch
):
    spec, path, new = monitor_job(tmp_path, valid_config)
    old = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setattr(
        "vaultkeep.scheduling.subprocess.run",
        lambda *args, **kwargs: pytest.fail("refresh must not invoke systemctl"),
    )
    plan = maintenance.plan(spec)
    assert path.read_bytes() == old[0]
    maintenance.apply(plan)
    assert path.read_text() == new
    assert path.stat().st_mode & 0o777 == 0o600
    maintenance.apply(plan, restore=True)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == old


@pytest.mark.parametrize("user", [False, True])
def test_refresh_refuses_schedule_overrides(tmp_path, valid_config, monkeypatch, user):
    spec, paths, _ = scheduled_job(tmp_path, valid_config, monkeypatch, user=user)
    override = paths.units_root / "vaultkeep@app.service.d/custom.conf"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("[Service]\nEnvironment=CUSTOM=1\n")
    with pytest.raises(ValueError, match="overrides require review"):
        maintenance.plan(spec)


def test_refresh_refuses_edits_inside_generated_monitor(tmp_path, valid_config):
    spec, path, _ = monitor_job(tmp_path, valid_config)
    path.write_text(path.read_text().replace("UMask=0077", "UMask=0022"))
    with pytest.raises(ValueError, match="locally modified"):
        maintenance.plan(spec)


def test_refresh_refuses_monitor_identity_change(tmp_path, valid_config):
    spec, path, _ = monitor_job(tmp_path, valid_config)
    path.rename(path.with_name("vaultkeep-trigger@other-identity.service"))
    # Marker and name must both match; renaming cannot adopt another job.
    with pytest.raises(ValueError, match="unmanaged"):
        maintenance.plan(spec)


@pytest.mark.parametrize("restore", [False, True])
def test_concurrent_edit_is_never_overwritten(tmp_path, valid_config, restore):
    spec, path, _ = monitor_job(tmp_path, valid_config)
    plan = maintenance.plan(spec)
    path.write_text("operator changes\n")
    with pytest.raises(ValueError, match="changed during maintenance"):
        maintenance.apply(plan, restore=restore)
    assert path.read_text() == "operator changes\n"


def test_edit_during_rendering_is_never_adopted_as_an_original_snapshot(
    tmp_path, valid_config, monkeypatch
):
    from vaultkeep.workflow import load_validated_config

    spec, path, _ = monitor_job(tmp_path, valid_config)

    def validate_and_edit(*args, **kwargs):
        config = load_validated_config(*args, **kwargs)
        path.write_text("concurrent operator edit\n")
        return config

    monkeypatch.setattr("vaultkeep.workflow.load_validated_config", validate_and_edit)
    plan = maintenance.plan(spec)
    with pytest.raises(ValueError, match="changed during maintenance"):
        maintenance.apply(plan)
    assert path.read_text() == "concurrent operator edit\n"


def test_unit_symlink_is_not_followed(tmp_path, valid_config):
    spec, path, _ = monitor_job(tmp_path, valid_config)
    target = path.with_name("private-file")
    path.rename(target)
    path.symlink_to(target)
    original = target.read_bytes()
    with pytest.raises(OSError):
        maintenance.plan(spec)
    assert target.read_bytes() == original


def test_validation_checks_runtime_only_for_previously_active_jobs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("vaultkeep.workflow.load_validated_config", lambda *a, **kw: None)
    monkeypatch.setattr("vaultkeep.workflow.validate_job", lambda path, **kw: calls.append(path))
    jobs = [
        {"config": str(tmp_path / name), "params": {}, "unit": unit, "monitor": False}
        for name, unit in (("a.yaml", "a.timer"), ("b.yaml", "b.timer"))
    ]
    maintenance.validate({"jobs": jobs, "active": ["a.timer"]})
    assert calls == [tmp_path / "a.yaml"]


def test_restore_continues_after_an_individual_failure(tmp_path, valid_config):
    spec, path, _ = monitor_job(tmp_path, valid_config)
    plan = maintenance.plan(spec)
    another = path.with_name("another.service")
    another.write_bytes(path.read_bytes())
    second = copy.deepcopy(plan["changes"][0])
    second["path"] = str(another)
    plan["changes"].append(second)
    maintenance.apply(plan)
    path.write_text("concurrent edit")
    with pytest.raises(ValueError, match="changed during maintenance"):
        maintenance.apply(plan, restore=True)
    assert another.read_bytes() != path.read_bytes()
    assert another.read_bytes() == base64.b64decode(second["before"]["content"])


def test_deployment_lock_prevents_code_replacement_during_a_command(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULTKEEP_INSTALL_PREFIX", str(tmp_path))
    path = tmp_path / "maintenance.lock"
    path.touch()
    with deployment_lock(), path.open("rb") as installer, pytest.raises(BlockingIOError):
        fcntl.flock(installer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    with path.open("rb") as installer:
        fcntl.flock(installer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LockError, match="in progress"), deployment_lock():
            pytest.fail("must not enter during maintenance")
    with deployment_lock():
        pass


@pytest.mark.parametrize("command,code", [("run", 10), ("verify", 10), ("trigger", 3)])
def test_cli_blocks_backup_and_trigger_during_maintenance(tmp_path, monkeypatch, command, code):
    monkeypatch.setenv("VAULTKEEP_INSTALL_PREFIX", str(tmp_path))
    path = tmp_path / "maintenance.lock"
    path.touch()
    args = ["--user", "--config", "/unused.yaml", command]
    if command == "trigger":
        args.append("monitor")
    with path.open("rb") as installer:
        fcntl.flock(installer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert main(args) == code
        assert main(["--version"]) == 0


def test_installed_entry_point_checks_maintenance_before_loading_backup_code(tmp_path):
    path = tmp_path / "maintenance.lock"
    path.touch()
    script = (
        "import sys; from vaultkeep.cli import main; "
        "assert 'vaultkeep.workflow' not in sys.modules; "
        "code = main(['--config', '/unused.yaml', 'run']); "
        "assert 'vaultkeep.workflow' not in sys.modules; raise SystemExit(code)"
    )
    with path.open("rb") as installer:
        fcntl.flock(installer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [sys.executable, "-I", "-c", script],
            env={**os.environ, "VAULTKEEP_INSTALL_PREFIX": str(tmp_path)},
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    assert result.returncode == 10, result.stderr
    assert "installation/update is in progress" in result.stderr


def test_missing_lock_allows_development_but_symlink_lock_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULTKEEP_INSTALL_PREFIX", str(tmp_path))
    with deployment_lock():
        pass
    (tmp_path / "maintenance.lock").symlink_to(tmp_path / "missing")
    with pytest.raises(LockError, match="Cannot open"), deployment_lock():
        pass
