"""Installed-job refresh and deployment-lock regressions, without live managers."""

from __future__ import annotations

import base64
import copy
import fcntl
import json
import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

from vaultkeep import maintenance
from vaultkeep.cli import main
from vaultkeep.errors import LockError
from vaultkeep.locking import deployment_lock
from vaultkeep.schedule import TimerManager, TimerPaths
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
    monkeypatch.setattr(
        "vaultkeep.schedule.subprocess.run",
        lambda *args, **kwargs: pytest.fail("maintenance must not invoke external commands"),
    )
    with monkeypatch.context() as installation:
        installation.setattr(manager, "_systemctl", lambda *args, **kwargs: "")
        # Schedule tests cover staged validation; this fixture only seeds managed files.
        installation.setattr(manager, "_validate_change", lambda *args: None)
        manager.install(config, runtime_params=params)
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


@pytest.mark.parametrize("user", [False, True])
@pytest.mark.parametrize("problem", ["obsolete", "malformed", "missing", "required", "unreadable"])
def test_configuration_check_reports_managed_job_failures(
    tmp_path, valid_config, monkeypatch, user, problem
):
    spec, _, config = scheduled_job(tmp_path, valid_config, monkeypatch, user=user)
    if problem == "obsolete":
        valid_config["trigger"] = {"notifications": {}}
        valid_config["destination"]["obsolete"] = True
        config.write_text(json.dumps(valid_config))
    elif problem == "malformed":
        config.write_text("job: [\n")
    elif problem == "missing":
        config.unlink()
    elif problem == "required":
        del valid_config["destination"]["root"]
        config.write_text(json.dumps(valid_config))
    else:
        original = Path.open

        def denied(path, *args, **kwargs):
            if path == config:
                raise PermissionError("configuration is not readable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)

    result = maintenance.check_configurations(spec)

    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["job"] == "app" and error["file"] == str(config)
    if problem == "obsolete":
        assert {issue["path"] for issue in error["issues"]} == {
            "trigger.notifications",
            "destination.obsolete",
        }
        # Refresh-time schema validation remains a safeguard.
        from vaultkeep.errors import ConfigurationError

        with pytest.raises(ConfigurationError):
            maintenance.plan(spec)
    elif problem == "required":
        assert error["issues"][0]["path"] == "destination.root"
    else:
        assert error["issues"][0]["path"] == ("job" if problem == "malformed" else "<root>")
        expected = {
            "missing": "Configuration file does not exist",
            "unreadable": "Cannot read configuration file",
            "malformed": "Invalid YAML syntax",
        }
        assert expected[problem] in error["issues"][0]["message"]


@pytest.mark.parametrize(
    "section,field,value,visible",
    [
        ("trigger", "poll_seconds", -1, True),
        ("trigger", "poll_seconds", "not-a-number", True),
        ("trigger", "poll_seconds", [1, 2], True),
        ("trigger", "poll_seconds", "private-token-never-print", False),
        ("encryption", "password_file", 12345, False),
        ("encryption", "mode", "unsupported-mode", True),
    ],
)
def test_configuration_report_includes_safe_invalid_values(
    tmp_path, valid_config, section, field, value, visible
):
    from vaultkeep.config import load_config
    from vaultkeep.errors import ConfigurationError

    path = tmp_path / "app.yaml"
    valid_config.setdefault(section, {})[field] = value
    path.write_text(json.dumps(valid_config))
    with pytest.raises(ConfigurationError) as caught, maintenance.configuration_context(path):
        load_config(path)
    details = maintenance.configuration_details(caught.value)
    assert details["file"] == str(path.absolute())
    problem = next(issue for issue in details["issues"] if issue["path"] == f"{section}.{field}")
    assert ("invalid value:" in problem["message"]) == visible
    if visible:
        assert json.dumps(value) in problem["message"]
    else:
        assert str(value) not in problem["message"]


def test_malformed_yaml_reports_location_and_enclosing_property_without_source(tmp_path):
    from vaultkeep.config import load_config
    from vaultkeep.errors import ConfigurationError

    path = tmp_path / "malformed.yaml"
    path.write_text("encryption:\n  password_file: [private-token, }\n")
    with pytest.raises(ConfigurationError) as caught, maintenance.configuration_context(path):
        load_config(path)
    details = maintenance.configuration_details(caught.value)
    problem = details["issues"][0]
    assert problem["path"] == "encryption.password_file"
    assert "line 2, column" in problem["message"]
    assert "private-token" not in json.dumps(details)


def test_recursive_invalid_value_does_not_replace_configuration_error(tmp_path, valid_config):
    from vaultkeep.config import load_config
    from vaultkeep.errors import ConfigurationError

    path = tmp_path / "recursive.yaml"
    path.write_text(json.dumps(valid_config) + "\n")
    # Keep the rest of the valid document while adding a recursive field value.
    content = path.read_text().rstrip()
    path.write_text(content[:-1] + ', "trigger": {"poll_seconds": &cycle [*cycle]}}\n')
    with pytest.raises(ConfigurationError) as caught, maintenance.configuration_context(path):
        load_config(path)
    details = maintenance.configuration_details(caught.value)
    assert details["issues"][0]["path"] == "trigger.poll_seconds"
    assert "Input should be a valid number" in details["issues"][0]["message"]


@pytest.mark.parametrize("failure", ["missing_unit", "internal"])
def test_account_process_reports_failures_as_json_and_retains_traceback(
    tmp_path, valid_config, monkeypatch, failure
):
    with monkeypatch.context() as setup:
        spec, paths, _ = scheduled_job(tmp_path, valid_config, setup, user=False)
    if failure == "missing_unit":
        paths.registry_path.unlink()
        expected = "timer ownership registry requires review:"
    else:
        spec = {}
        expected = "Internal managed-job failure; see diagnostics log for technical details."
    result = subprocess.run(
        [sys.executable, "-I", "-m", "vaultkeep.maintenance", "plan"],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    message = json.loads(result.stdout)["failure"]
    assert expected in message
    assert "Traceback" in result.stderr
    assert "KeyError" not in message and "ValueError" not in message


def test_configuration_check_includes_custom_monitor_paths_and_all_directory_jobs(
    tmp_path, valid_config
):
    spec, unit, _ = monitor_job(tmp_path, valid_config)
    custom = tmp_path / "directory with spaces/app.yaml"
    custom.write_text("job: [\n")
    jobs = Path(spec["paths"]["jobs_root"])
    jobs.mkdir()
    (jobs / "another.yaml").write_text("{}")
    (jobs / "ignored.example").write_text("invalid YAML: [")
    before = unit.read_bytes()

    result = maintenance.check_configurations(spec)

    assert {error["file"] for error in result["errors"]} == {
        str(custom),
        str(jobs / "another.yaml"),
    }
    assert unit.read_bytes() == before


@pytest.mark.parametrize("user", [False, True])
def test_configuration_check_accepts_valid_parameterized_jobs(
    tmp_path, valid_config, monkeypatch, user
):
    spec, _, _ = scheduled_job(
        tmp_path, valid_config, monkeypatch, user=user, params={"repo": "nas"}
    )
    assert maintenance.check_configurations(spec) == {"errors": []}


def monitor_job(tmp_path, valid_config):
    spec = document(tmp_path, user=True)
    root = Path(spec["unit_roots"][0])
    root.mkdir()
    valid_config["application"] = {
        "name": "electrum",
        "data_dir": "/home/user/.electrum",
        "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/electrum"}]},
        "user": pwd.getpwuid(os.geteuid()).pw_name,
    }
    valid_config["trigger"] = {"enabled": True}
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
        "vaultkeep.schedule.subprocess.run",
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
def test_refresh_migrates_default_schedule_binary_to_custom_layout(
    tmp_path, valid_config, monkeypatch, user
):
    spec, paths, _ = scheduled_job(
        tmp_path, valid_config, monkeypatch, user=user, params={"repo": "nas"}
    )
    service = next(paths.units_root.rglob("*.service" if user else "override.conf"))
    original = service.read_bytes()
    monkeypatch.setenv("VAULTKEEP_BIN_LINK", "/custom/bin/vaultkeep")
    plan = maintenance.plan(spec)
    maintenance.apply(plan)
    assert "ExecStart=/custom/bin/vaultkeep " in service.read_text()
    # The regenerated custom command must remain refreshable.
    maintenance.plan(spec)
    maintenance.apply(plan, restore=True)
    assert service.read_bytes() == original


def test_refresh_migrates_default_monitor_binary_to_custom_layout(
    tmp_path, valid_config, monkeypatch
):
    spec, path, _ = monitor_job(tmp_path, valid_config)
    original = path.read_bytes()
    monkeypatch.setenv("VAULTKEEP_BIN_LINK", "/custom/bin/vaultkeep")
    plan = maintenance.plan(spec)
    maintenance.apply(plan)
    assert "ExecStart=/custom/bin/vaultkeep --user" in path.read_text()
    maintenance.plan(spec)
    maintenance.apply(plan, restore=True)
    assert path.read_bytes() == original


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
    with pytest.raises(ValueError, match="locally modified") as caught:
        maintenance.plan(spec)
    assert str(path) in str(caught.value)


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
    from vaultkeep.backup.workflow import load_validated_config

    spec, path, _ = monitor_job(tmp_path, valid_config)

    def validate_and_edit(*args, **kwargs):
        config = load_validated_config(*args, **kwargs)
        path.write_text("concurrent operator edit\n")
        return config

    monkeypatch.setattr("vaultkeep.backup.workflow.load_validated_config", validate_and_edit)
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
    monkeypatch.setattr("vaultkeep.backup.workflow.load_validated_config", lambda *a, **kw: None)
    monkeypatch.setattr(
        "vaultkeep.backup.workflow.validate_job", lambda path, **kw: calls.append(path)
    )
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
        "assert 'vaultkeep.backup.workflow' not in sys.modules; "
        "code = main(['--config', '/unused.yaml', 'run']); "
        "assert 'vaultkeep.backup.workflow' not in sys.modules; raise SystemExit(code)"
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


def application_identity(user):
    return {
        "name": "bisq",
        "user": user,
        "data_dir": "/data/bisq/instance",
        "matcher": {"entrypoints": [{"kind": "script", "value": "/opt/bisq/bin/Bisq"}]},
    }


@pytest.mark.parametrize("user", [False, True])
def test_configuration_check_resolves_disabled_job_identities_without_mutation(
    tmp_path, valid_config, monkeypatch, user
):
    spec, paths, config = scheduled_job(tmp_path, valid_config, monkeypatch, user=user)
    valid_config["application"] = application_identity("vaultkeep-missing-application-user")
    valid_config["consistency"] = {"mode": "application_offline"}
    valid_config["trigger"] = {"enabled": False}
    config.write_text(json.dumps(valid_config))
    # An unrelated valid job must not mask the invalid installed selection.
    other = paths.jobs_root / "other.yaml"
    other_config = copy.deepcopy(valid_config)
    other_config["job"]["id"] = "other"
    other_config.pop("application")
    other_config["consistency"]["mode"] = "files"
    other.write_text(json.dumps(other_config))
    originals = {
        path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    result = maintenance.check_configurations(spec)

    assert result["errors"] == [
        {
            "job": "app",
            "file": str(config.absolute()),
            "issues": [
                {
                    "path": "application.user",
                    "message": "User 'vaultkeep-missing-application-user' does not exist.",
                }
            ],
        }
    ]
    assert all(
        (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) == before
        for path, before in originals.items()
    )


@pytest.mark.parametrize("user", [False, True])
def test_configuration_check_validates_service_groups(tmp_path, valid_config, monkeypatch, user):
    spec, _, config = scheduled_job(tmp_path, valid_config, monkeypatch, user=user)

    def missing(value):
        raise KeyError(f"getgr lookup failed: {value}")

    monkeypatch.setattr(maintenance.grp, "getgrnam", missing)
    monkeypatch.setattr(maintenance.grp, "getgrgid", missing)
    result = maintenance.check_configurations(spec)
    error = result["errors"][0]
    assert error["file"] == str(config)
    issue = error["issues"][0]
    assert issue["path"] == "<root>"  # Execution groups are not YAML properties.
    value = f"GID {pwd.getpwuid(os.geteuid()).pw_gid}" if user else "'root'"
    assert issue["message"] == f"Group {value} does not exist (required by the backup service)."
    assert "getgr" not in issue["message"]


@pytest.mark.parametrize("user", [False, True])
def test_configuration_check_validates_service_users(tmp_path, valid_config, monkeypatch, user):
    spec, _, config = scheduled_job(tmp_path, valid_config, monkeypatch, user=user)

    def missing(value):
        raise KeyError(value)

    monkeypatch.setattr(maintenance.pwd, "getpwnam", missing)
    monkeypatch.setattr(maintenance.pwd, "getpwuid", missing)
    issue = maintenance.check_configurations(spec)["errors"][0]
    assert issue["file"] == str(config)
    assert "User " in issue["issues"][0]["message"]
    assert "does not exist" in issue["issues"][0]["message"]


@pytest.mark.parametrize("property", ["data_dir", "exe_realpath", "entrypoint"])
def test_application_path_failure_retains_actual_field(
    tmp_path, valid_config, monkeypatch, property
):
    spec = document(tmp_path, user=False)
    jobs = Path(spec["paths"]["jobs_root"])
    jobs.mkdir()
    config = jobs / "app.yaml"
    identity = application_identity(pwd.getpwuid(os.geteuid()).pw_name)
    broken = str(tmp_path / "loop")
    original_resolve = Path.resolve

    def denied(path, *args, **kwargs):
        if str(path) == broken:
            raise PermissionError("path traversal denied")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", denied)
    if property == "data_dir":
        identity["data_dir"] = broken
        field = "application.data_dir"
    elif property == "exe_realpath":
        identity["matcher"]["exe_realpath"] = broken
        field = "application.matcher.exe_realpath"
    else:
        identity["matcher"]["entrypoints"][0]["value"] = broken
        field = "application.matcher.entrypoints[0].value"
    valid_config["application"] = identity
    config.write_text(json.dumps(valid_config))

    result = maintenance.check_configurations(spec)
    assert result["errors"][0]["issues"][0]["path"] == field
    assert "symlinks" in result["errors"][0]["issues"][0]["message"]


def test_configuration_check_does_not_disguise_programming_errors(
    tmp_path, valid_config, monkeypatch
):
    spec, _, config = scheduled_job(tmp_path, valid_config, monkeypatch, user=False)
    valid_config["application"] = application_identity(pwd.getpwuid(os.geteuid()).pw_name)
    config.write_text(json.dumps(valid_config))

    def broken(*args, **kwargs):
        raise RuntimeError("identity implementation bug")

    monkeypatch.setattr("vaultkeep.application.resolve_application", broken)
    with pytest.raises(RuntimeError, match="identity implementation bug"):
        maintenance.check_configurations(spec)


def test_preflight_renders_saved_parameters_and_reports_configuration_path(
    tmp_path, valid_config, monkeypatch
):
    spec, _, config = scheduled_job(
        tmp_path, valid_config, monkeypatch, user=True, params={"repo": "nas"}
    )
    valid_config["destination"]["name_template"] = "backup-{unknown}"
    config.write_text(json.dumps(valid_config))
    result = maintenance.check_configurations(spec)
    assert result["errors"][0]["file"] == str(config)
    assert result["errors"][0]["issues"][0]["path"] == "destination.name_template"


def test_runtime_validation_retains_job_path_and_actionable_failure(tmp_path, monkeypatch):
    from vaultkeep.errors import DestinationError

    config = tmp_path / "app.yaml"
    monkeypatch.setattr("vaultkeep.backup.workflow.load_validated_config", lambda *a, **kw: None)

    def unavailable(*args, **kwargs):
        raise DestinationError("Configured source does not exist: /data/app")

    monkeypatch.setattr("vaultkeep.backup.workflow.validate_job", unavailable)
    with pytest.raises(DestinationError) as caught:
        maintenance.validate(
            {
                "jobs": [
                    {"config": str(config), "params": {}, "unit": "app.timer", "monitor": False}
                ],
                "active": ["app.timer"],
            }
        )
    assert maintenance.configuration_details(caught.value) == {
        "job": "app",
        "file": str(config),
        "issues": [{"path": "<root>", "message": "Configured source does not exist: /data/app"}],
    }


@pytest.mark.parametrize("user", [False, True])
def test_preflight_reports_disabled_installed_schedule_field(
    tmp_path, valid_config, monkeypatch, user
):
    spec, _, config = scheduled_job(tmp_path, valid_config, monkeypatch, user=user)
    valid_config["schedule"]["enabled"] = False
    config.write_text(json.dumps(valid_config))
    issue = maintenance.check_configurations(spec)["errors"][0]
    assert issue["file"] == str(config)
    assert issue["issues"][0]["path"] == "schedule.enabled"
    assert "false" in issue["issues"][0]["message"]


def test_preflight_reports_disabled_active_monitor_field(tmp_path, valid_config):
    spec, unit, _ = monitor_job(tmp_path, valid_config)
    config = tmp_path / "directory with spaces/app.yaml"
    valid_config["trigger"]["enabled"] = False
    config.write_text(json.dumps(valid_config))
    # Inactive monitors may remain installed with trigger.enabled false.
    assert maintenance.check_configurations(spec) == {"errors": []}
    spec["active"] = [unit.name]
    issue = maintenance.check_configurations(spec)["errors"][0]
    assert issue["file"] == str(config)
    assert issue["issues"][0]["path"] == "trigger.enabled"
    assert "false" in issue["issues"][0]["message"]
