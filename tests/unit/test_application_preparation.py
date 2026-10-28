"""Identity preparation, independent inspection, and retained safety boundaries."""

from __future__ import annotations

import io
import json
import os
import pwd
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tests.unit import test_backup_safety_redesign as safety
from vaultkeep import application as identities
from vaultkeep import maintenance, validation
from vaultkeep.application import (
    DetectionResult,
    Observation,
    ResolvedApplication,
    execution_policy,
    require_application_offline,
    resolve_application,
    validate_application_directory,
)
from vaultkeep.backup import workflow
from vaultkeep.config import JobConfig, load_config
from vaultkeep.config.models import ApplicationConfig
from vaultkeep.destination.identity import replacement_identity
from vaultkeep.errors import ApplicationIdentityError, SourceDiscoveryError
from vaultkeep.schedule import TimerManager, TimerPaths
from vaultkeep.sources.hashing import calculate_config_fingerprint
from vaultkeep.trigger.config import from_prepared_job

job = safety.job


def raw_application(directory="~/wallet", *, name="bisq", user=None):
    return ApplicationConfig(
        name=name,
        user=user,
        data_dir=str(directory),
        matcher={"entrypoints": [{"kind": "java_main", "value": "test.App"}]},
    )


def test_resolution_detaches_nested_configuration_and_serialized_snapshots():
    raw = raw_application(name="electrum")
    raw.matcher.argv_tokens.append("--wallet")
    raw.matcher.env_eq["ELECTRUMDIR"] = "/wallet"
    before = raw.model_dump()
    resolved = resolve_application(raw, user_mode=True)
    assert raw.model_dump() == before
    assert resolve_application(ApplicationConfig.model_validate(before), user_mode=True) == resolved
    snapshot = resolved.canonical()
    raw.matcher.entrypoints.clear()
    raw.matcher.argv_tokens.append("changed")
    raw.matcher.env_eq.clear()
    assert resolved.canonical() == snapshot
    snapshot["matcher"]["argv_tokens"].append("changed snapshot")
    assert resolved.matcher.argv_tokens == ["--wallet"]
    with pytest.raises(FrozenInstanceError):
        resolved.owner_uid = 99
    restored = ResolvedApplication(
        **(resolved.canonical() | {"matcher": resolved.matcher.model_copy(deep=True)}),
        owner_uid=resolved.owner_uid,
    )
    assert restored == resolved


@pytest.mark.parametrize("username", [pwd.getpwuid(os.geteuid()).pw_name, "missing-account"])
def test_unsupported_override_precedes_every_application_account_lookup(
    username, monkeypatch, tmp_path
):
    def unexpected(*_args):
        pytest.fail("unsupported override caused an account lookup")

    monkeypatch.setattr(identities.pwd, "getpwnam", unexpected)
    monkeypatch.setattr(identities.pwd, "getpwuid", unexpected)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ApplicationIdentityError) as caught:
        resolve_application(
            raw_application(user=username), user_mode=True, config_path=Path("app.yaml")
        )
    assert caught.value.issue.code == "user_unsupported"
    assert str(tmp_path / "app.yaml") in str(caught.value)
    assert "application.user" in str(caught.value)


def test_preparation_optional_and_required_application_contract(valid_config, tmp_path):
    raw = JobConfig.model_validate(valid_config)
    prepared, app = validation.prepare_job(raw, user_mode=True, config_path=tmp_path / "app.yaml")
    assert prepared == raw and app is None
    for section in (
        {"trigger": {"enabled": True}},
        {"consistency": {"mode": "application_offline"}},
    ):
        with pytest.raises(ValidationError, match="application is required"):
            JobConfig.model_validate(valid_config | section)


@pytest.mark.parametrize("operation", ["backup", "validate", "list", "verify", "prune", "schedule"])
def test_invalid_system_scope_fails_before_state_or_operations(
    operation, valid_config, tmp_path, monkeypatch
):
    monkeypatch.setattr(identities.os, "geteuid", lambda: 1001)
    path = tmp_path / "app.yaml"
    path.write_text(json.dumps(valid_config))
    paths = workflow.WorkflowPaths(tmp_path / "state", tmp_path / "cache", tmp_path / "locks")
    before = sorted(tmp_path.rglob("*"))
    calls = {
        "backup": lambda: workflow.run_backup(path, paths=paths),
        "validate": lambda: workflow.validate_job(path),
        "list": lambda: workflow.list_backups(path),
        "verify": lambda: workflow.verify_backups(path),
        "prune": lambda: workflow.prune_backups(path, dry_run=False),
        "schedule": lambda: TimerManager(
            TimerPaths(tmp_path, tmp_path / "units", tmp_path / "registry")
        ).install(path),
    }
    with pytest.raises(ApplicationIdentityError, match="System execution requires root"):
        calls[operation]()
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("system", [False, True])
@pytest.mark.parametrize(
    "enabled,parameterized", [(True, False), (False, False), (True, True), (False, True)]
)
@pytest.mark.parametrize("operation", ["install", "update", "validate_all", "sync"])
def test_schedule_identity_preflight_covers_disabled_and_deferred_jobs(
    system, enabled, parameterized, operation, valid_config, tmp_path, monkeypatch
):
    valid_config["schedule"]["enabled"] = enabled
    valid_config["application"] = raw_application(tmp_path, user="missing-account").model_dump()
    if parameterized:
        valid_config["destination"]["root"] += "/{repo}"
    path = tmp_path / "app.yaml"
    path.write_text(json.dumps(valid_config))
    manager = TimerManager(
        TimerPaths(tmp_path, tmp_path / "units", tmp_path / "registry"), user_mode=not system
    )
    monkeypatch.setattr(identities.os, "geteuid", lambda: 0 if system else 1001)
    if system:

        def missing(_name):
            raise KeyError("missing-account")

        monkeypatch.setattr(identities.pwd, "getpwnam", missing)
    monkeypatch.setattr(
        manager, "_run", lambda *_a, **_kw: pytest.fail("identity error reached systemd")
    )
    before = sorted(tmp_path.rglob("*"))
    with pytest.raises(ApplicationIdentityError) as caught:
        if operation in {"install", "update"}:
            getattr(manager, operation)(path)
        else:
            getattr(manager, operation)()
    assert caught.value.issue.code == ("user_missing" if system else "user_unsupported")
    assert str(path) in str(caught.value)
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("backup_mode", ["combined", "per_file"])
def test_backup_prepares_once_and_preserves_independent_offline_checks(
    job, backup_mode, monkeypatch
):
    safety.offline(job)
    safety.configure(job, backup_mode=backup_mode)
    resolver = validation.resolve_application
    resolve_calls = []
    checks = []
    offline = workflow.require_application_offline

    def resolve(*args, **kwargs):
        result = resolver(*args, **kwargs)
        resolve_calls.append(result)
        return result

    def check(config, *, application):
        checks.append(application)
        offline(config, application=application)

    monkeypatch.setattr(validation, "resolve_application", resolve)
    monkeypatch.setattr(workflow, "require_application_offline", check)
    monkeypatch.setattr(
        identities, "detect_spec", lambda **_kw: DetectionResult(Observation.STOPPED)
    )
    assert safety.run(job).result == "created"
    assert len(resolve_calls) == 1
    assert len(checks) >= 3
    assert all(app is resolve_calls[0] for app in checks)


@pytest.mark.parametrize("failure", ["removed", "remapped", "ownership", "symlink"])
def test_offline_revalidation_fails_closed_after_preparation(job, monkeypatch, failure):
    raw = safety.offline(job)
    _, app = validation.prepare_job(raw, user_mode=True, config_path=job)
    monkeypatch.setattr(
        identities,
        "detect_spec",
        lambda **_kw: pytest.fail("changed identity reached process inspection"),
    )
    if failure == "removed":

        def removed(_name):
            raise KeyError("account removed")

        monkeypatch.setattr(identities.pwd, "getpwnam", removed)
    elif failure == "remapped":
        monkeypatch.setattr(
            identities.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=app.owner_uid + 1)
        )
    elif failure == "ownership":
        original = Path.stat

        def foreign(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            return (
                SimpleNamespace(st_mode=info.st_mode, st_uid=app.owner_uid + 1)
                if str(path) == app.data_dir
                else info
            )

        monkeypatch.setattr(Path, "stat", foreign)
    else:
        directory = Path(app.data_dir)
        directory.rename(directory.with_name("moved"))
        directory.symlink_to(directory.with_name("moved"), target_is_directory=True)
    with pytest.raises(SourceDiscoveryError, match="identity is unavailable or changed"):
        require_application_offline(raw, application=app)


@pytest.mark.parametrize("backup_mode", ["combined", "per_file"])
@pytest.mark.parametrize("account_error", [KeyError, OSError])
def test_inspection_survives_unavailable_application_source_and_account(
    job, monkeypatch, backup_mode, account_error
):
    safety.offline(job)
    safety.configure(job, backup_mode=backup_mode)
    monkeypatch.setattr(
        identities, "detect_spec", lambda **_kw: DetectionResult(Observation.STOPPED)
    )
    result = safety.run(job)
    raw = load_config(job)
    for child in Path(raw.sources[0].path).iterdir():
        child.unlink()
    Path(raw.sources[0].path).rmdir()
    document = json.loads(job.read_text())
    document["application"]["user"] = "removed-account"
    job.write_text(json.dumps(document))
    monkeypatch.setattr(identities.os, "geteuid", lambda: 0)

    lookups = []

    def unavailable(name):
        lookups.append(name)
        raise account_error("application account unavailable")

    monkeypatch.setattr(identities.pwd, "getpwnam", unavailable)
    inspected = []
    monkeypatch.setattr(workflow, "verify_tar_zstd", lambda path, **_kw: inspected.append(path))
    inventory = workflow.list_backups(job)
    assert len(inventory.backups) == 1
    assert inventory.retention_warnings
    assert lookups == ["removed-account"]
    assert workflow.verify_backups(job).backups == 1
    assert lookups == ["removed-account"]  # Integrity verification performs no account lookup.
    assert len(inspected) == 1
    # The same unavailable identity must never authorize deletion, including preview.
    for dry_run in (False, True):
        with pytest.raises(ApplicationIdentityError) as caught:
            workflow.prune_backups(job, dry_run=dry_run)
        assert caught.value.issue.code == (
            "user_lookup" if account_error is OSError else "user_missing"
        )
    archive = result.files[0].archive_path if result.files else result.archive_path
    assert archive.is_file()


def test_fingerprint_and_replacement_identity_match_pre_refactor_golden_values(
    valid_config, monkeypatch, tmp_path
):
    # These expected values were captured from the pre-refactor working tree.
    monkeypatch.setattr(identities.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(
        identities.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_uid=1001, pw_name="alice", pw_dir="/home/alice"),
    )
    valid_config["application"] = raw_application().model_dump(exclude_none=True)
    valid_config["consistency"] = {"mode": "application_offline"}
    config, app = validation.prepare_job(
        JobConfig.model_validate(valid_config), user_mode=True, config_path=tmp_path / "app.yaml"
    )
    assert (
        calculate_config_fingerprint(config, application=app)
        == "sha256:fd4601f1cb34d28ff40b5aabb0f9fb16bf40d421e812ce457602306bdfb9da92"
    )
    history = replacement_identity(config, application=app)
    assert history.application_user == "alice"
    assert history.application_data_dir == "/home/alice/wallet"
    assert history.model_dump().keys() == {
        "identity_version",
        "source_scope",
        "consistency_mode",
        "application_name",
        "application_user",
        "application_data_dir",
    }
    renamed = ResolvedApplication("bisq", "renamed", app.owner_uid, app.data_dir, app.matcher)
    recreated = ResolvedApplication("bisq", "alice", app.owner_uid + 1, app.data_dir, app.matcher)
    assert replacement_identity(config, application=recreated) == history
    assert replacement_identity(config, application=renamed) != history
    assert calculate_config_fingerprint(
        config, application=recreated
    ) != calculate_config_fingerprint(config, application=app)
    trigger = from_prepared_job(config, app, source=tmp_path / "app.yaml", params={})
    assert trigger.instances[0].execution_policy == execution_policy(config, app)
    assert trigger.instances[0].owner_uid == 1001


def test_directory_readiness_reuses_prepared_identity(tmp_path, monkeypatch):
    resolved = resolve_application(raw_application(tmp_path), user_mode=True)
    monkeypatch.setattr(
        identities.pwd, "getpwuid", lambda *_a: pytest.fail("directory validation resolved again")
    )
    validate_application_directory(resolved)


@pytest.mark.parametrize("uid", [0, 1001])
def test_default_account_must_match_validated_execution_uid(monkeypatch, uid):
    monkeypatch.setattr(identities.os, "geteuid", lambda: uid)
    monkeypatch.setattr(
        identities.pwd,
        "getpwuid",
        lambda _uid: SimpleNamespace(pw_uid=uid + 1, pw_name="foreign", pw_dir="/foreign"),
    )
    with pytest.raises(ApplicationIdentityError) as caught:
        resolve_application(raw_application(), user_mode=uid != 0)
    assert caught.value.issue.code == "user_mismatch"


def test_identity_and_preparation_imports_do_not_load_trigger():
    for imports in (
        ("vaultkeep.application", "vaultkeep.validation"),
        ("vaultkeep.validation", "vaultkeep.application"),
    ):
        script = (
            "import importlib, sys; "
            f"[importlib.import_module(name) for name in {imports!r}]; "
            "assert not any(name.startswith('vaultkeep.trigger') for name in sys.modules)"
        )
        subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, timeout=10)


@pytest.fixture
def maintenance_document(tmp_path):
    return {
        "user": False,
        "jobs": [],
        "active": [],
        "unit_roots": [str(tmp_path / "units")],
        "paths": {
            "jobs_root": str(tmp_path / "jobs"),
            "units_root": str(tmp_path / "units"),
            "registry_path": str(tmp_path / "registry.json"),
        },
    }


@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.parametrize(
    "entry,action",
    [
        ("function", "check_configurations"),
        ("function", "plan"),
        ("function", "validate"),
        ("main", "check-configurations"),
        ("main", "plan"),
        ("main", "validate"),
        ("main", "check-launchers"),
    ],
)
def test_maintenance_system_scope_precedes_inventory_and_account_checks(
    maintenance_document, tmp_path, monkeypatch, capsys, populated, entry, action
):
    if populated:
        units = tmp_path / "units" / "vaultkeep@broken.timer.d"
        units.mkdir(parents=True)
        (units / "schedule.conf").write_text("invalid launcher\n")
        (tmp_path / "registry.json").write_text("{invalid registry")
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    def unexpected(*_args, **_kwargs):
        pytest.fail("invalid execution scope reached inventory or account lookup")

    monkeypatch.setattr(identities.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(identities.pwd, "getpwnam", unexpected)
    monkeypatch.setattr(identities.pwd, "getpwuid", unexpected)
    monkeypatch.setattr(maintenance, "managed_units", unexpected)
    if entry == "function":
        with pytest.raises(ApplicationIdentityError) as caught:
            getattr(maintenance, action)(maintenance_document)
        assert caught.value.issue.code == "execution_scope"
    else:
        monkeypatch.setattr(sys, "argv", ["vaultkeep.maintenance", action])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(maintenance_document)))
        with pytest.raises(SystemExit) as caught:
            maintenance.main()
        assert caught.value.code == 1
        assert json.loads(capsys.readouterr().out) == {
            "failure": "System execution requires root; use sudo or select --user."
        }
    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (tmp_path / "jobs").exists()


@pytest.mark.parametrize("user,uid", [(False, 0), (True, 1001)])
@pytest.mark.parametrize("action", ["check_configurations", "plan", "validate"])
def test_empty_maintenance_accepts_valid_execution_scope_without_account_lookup(
    maintenance_document, monkeypatch, user, uid, action
):
    maintenance_document["user"] = user
    monkeypatch.setattr(identities.os, "geteuid", lambda: uid)

    def unexpected(*_args):
        pytest.fail("empty maintenance request looked up an application account")

    monkeypatch.setattr(identities.pwd, "getpwnam", unexpected)
    monkeypatch.setattr(identities.pwd, "getpwuid", unexpected)
    result = getattr(maintenance, action)(maintenance_document)
    assert (
        result
        == {
            "check_configurations": {"errors": []},
            "plan": {"changes": [], "jobs": []},
            "validate": None,
        }[action]
    )


def test_maintenance_cli_rejects_system_scope_with_actionable_json(tmp_path):
    script = (
        "import os, runpy, sys; os.geteuid = lambda: 1001; "
        "sys.argv = ['vaultkeep.maintenance', 'check-launchers']; "
        "runpy.run_module('vaultkeep.maintenance', run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        input=json.dumps({"user": False}),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "failure": "System execution requires root; use sudo or select --user."
    }


@pytest.mark.parametrize("operation", ["update", "sync"])
def test_schedule_identity_failure_preserves_installed_files_and_registry(
    operation, valid_config, tmp_path, monkeypatch
):
    path = tmp_path / "app.yaml"
    valid_config["schedule"]["enabled"] = True
    valid_config["application"] = raw_application(tmp_path).model_dump()
    path.write_text(json.dumps(valid_config))
    manager = TimerManager(
        TimerPaths(tmp_path, tmp_path / "units", tmp_path / "registry.json"), user_mode=True
    )
    monkeypatch.setattr(manager, "_systemctl", lambda *_a, **_kw: "")
    monkeypatch.setattr(manager, "_validate_change", lambda *_a: None)
    manager.install(path)
    valid_config["application"]["user"] = "unsupported-account"
    path.write_text(json.dumps(valid_config))
    before = {
        file: (file.read_bytes(), file.stat().st_mode, file.stat().st_mtime_ns)
        for file in tmp_path.rglob("*")
        if file.is_file()
    }

    def unexpected(*_args, **_kwargs):
        pytest.fail("identity failure reached staging, systemd, or transaction application")

    monkeypatch.setattr(manager, "_run", unexpected)
    monkeypatch.setattr(manager, "_systemctl", unexpected)
    monkeypatch.setattr(manager, "_validate_change", unexpected)
    monkeypatch.setattr(manager, "_apply_sync", unexpected)
    with pytest.raises(ApplicationIdentityError) as caught:
        manager.update(path) if operation == "update" else manager.sync()
    assert caught.value.issue.code == "user_unsupported"
    assert {
        file: (file.read_bytes(), file.stat().st_mode, file.stat().st_mtime_ns)
        for file in tmp_path.rglob("*")
        if file.is_file()
    } == before
