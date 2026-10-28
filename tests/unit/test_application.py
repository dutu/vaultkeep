"""Execution-account resolution and isolation of application process observations."""

from __future__ import annotations

import json
import os
import pwd
from types import SimpleNamespace

import pytest

from vaultkeep.application import (
    Observation,
    PsutilProcessProvider,
    detect_spec,
    execution_policy,
    matcher_from_canonical,
    require_application_offline,
    resolve_application,
    validate_application_directory,
)
from vaultkeep.config.models import ApplicationConfig, JobConfig
from vaultkeep.destination.identity import replacement_identity
from vaultkeep.errors import ApplicationIdentityError, SourceDiscoveryError
from vaultkeep.trigger.config import load_config


@pytest.fixture
def application(tmp_path):
    return ApplicationConfig(
        name="bisq",
        data_dir=str(tmp_path),
        matcher={"entrypoints": [{"kind": "script", "value": "/opt/bisq/Bisq"}]},
    )


def test_omitted_account_uses_execution_uid_and_account_home(application, monkeypatch):
    owner = pwd.getpwuid(os.geteuid())
    for key in ("HOME", "USER", "LOGNAME", "SUDO_USER"):
        monkeypatch.setenv(key, "/decoy")
    application = application.model_copy(
        update={
            "data_dir": "~/wallet",
            "matcher": application.matcher.model_copy(
                update={
                    "exe_realpath": "~/bin/java",
                    "entrypoints": [
                        application.matcher.entrypoints[0].model_copy(update={"value": "~/Bisq"})
                    ],
                }
            ),
        }
    )

    resolved = resolve_application(application, user_mode=os.geteuid() != 0)
    uid = resolved.owner_uid

    assert uid == owner.pw_uid
    assert resolved.user == owner.pw_name
    assert resolved.data_dir == owner.pw_dir + "/wallet"
    assert resolved.matcher.exe_realpath == owner.pw_dir + "/bin/java"
    assert resolved.matcher.entrypoints[0].value == owner.pw_dir + "/Bisq"
    assert resolve_application(application, user_mode=os.geteuid() != 0) == resolved


@pytest.mark.parametrize("explicit", [False, True])
def test_root_defaults_to_root_and_can_select_another_owner(application, monkeypatch, explicit):
    owner = pwd.getpwuid(os.geteuid())
    monkeypatch.setattr("vaultkeep.application.os.geteuid", lambda: 0)
    if explicit:
        application = application.model_copy(update={"user": owner.pw_name})

    resolved = resolve_application(application, user_mode=os.geteuid() != 0)
    uid = resolved.owner_uid

    expected = owner if explicit else pwd.getpwuid(0)
    assert uid == expected.pw_uid
    assert resolved.user == expected.pw_name


def test_implicit_root_identity_matches_explicit_identity(application, valid_config, monkeypatch):
    monkeypatch.setattr("vaultkeep.application.os.geteuid", lambda: 0)
    implicit = resolve_application(application, user_mode=False)
    explicit = resolve_application(
        application.model_copy(update={"user": pwd.getpwuid(0).pw_name}), user_mode=False
    )
    # Both spellings resolve to the same frozen worker policy and manifest owner.
    config = JobConfig.model_validate(valid_config).model_copy(update={"application": application})
    assert execution_policy(config, implicit) == execution_policy(config, explicit)
    offline = config.model_copy(
        update={
            "consistency": config.consistency.model_copy(update={"mode": "application_offline"})
        }
    )
    assert replacement_identity(offline, application=implicit) == replacement_identity(
        offline, application=explicit
    )


@pytest.mark.parametrize("target", ["self", "another"])
def test_user_execution_rejects_explicit_account_with_field_and_file(
    application, tmp_path, monkeypatch, target
):
    owner = pwd.getpwuid(os.geteuid())
    monkeypatch.setattr("vaultkeep.application.os.geteuid", lambda: owner.pw_uid or 1234)
    name = owner.pw_name if target == "self" else pwd.getpwuid(0).pw_name
    path = tmp_path / "app.yaml"

    with pytest.raises(ApplicationIdentityError) as caught:
        resolve_application(
            application.model_copy(update={"user": name}), config_path=path, user_mode=True
        )

    assert caught.value.issue.code == "user_unsupported"
    assert str(path) in str(caught.value)
    assert "application.user" in str(caught.value)
    assert "remove application.user" in str(caught.value)


@pytest.mark.parametrize("error", [KeyError(1234), OSError("account service unavailable")])
def test_missing_execution_account_fails_closed(application, monkeypatch, error):
    def unavailable(_uid):
        raise error

    monkeypatch.setattr("vaultkeep.application.pwd.getpwuid", unavailable)
    with pytest.raises(ApplicationIdentityError, match=r"application\.user"):
        resolve_application(application, user_mode=True)


def test_foreign_owned_directory_is_rejected(application, monkeypatch):
    owner = pwd.getpwuid(os.geteuid())
    foreign = SimpleNamespace(pw_uid=owner.pw_uid + 1, pw_name="foreign", pw_dir="/foreign")
    monkeypatch.setattr("vaultkeep.application.os.geteuid", lambda: 0)
    monkeypatch.setattr("vaultkeep.application.pwd.getpwnam", lambda _name: foreign)
    with pytest.raises(ApplicationIdentityError, match=r"application\.data_dir.*owned by"):
        validate_application_directory(
            resolve_application(application.model_copy(update={"user": "foreign"}), user_mode=False)
        )


@pytest.mark.parametrize("executor_uid,target_uid", [(1001, 1001), (0, 0), (0, 1002)])
def test_derived_owner_isolates_similar_processes_and_start_stop_observations(
    application, monkeypatch, executor_uid, target_uid
):
    account = SimpleNamespace(pw_uid=target_uid, pw_name="target", pw_dir="/home/target")
    monkeypatch.setattr("vaultkeep.application.os.geteuid", lambda: executor_uid)
    monkeypatch.setattr("vaultkeep.application.pwd.getpwuid", lambda _uid: account)
    monkeypatch.setattr("vaultkeep.application.pwd.getpwnam", lambda _name: account)
    if executor_uid != target_uid:
        application = application.model_copy(update={"user": "target"})
    resolved = resolve_application(application, user_mode=os.geteuid() != 0)
    uid = resolved.owner_uid

    def process(pid, owner):
        return SimpleNamespace(
            pid=pid,
            info={"uids": SimpleNamespace(real=owner)},
            create_time=lambda: 1.0,
            exe=lambda: "/opt/bisq/Bisq",
            cmdline=lambda: ["/opt/bisq/Bisq", "--appDataDir", resolved.data_dir],
        )

    processes = [process(42, target_uid), process(99, target_uid + 1)]
    monkeypatch.setattr("vaultkeep.application.psutil.process_iter", lambda _attrs: processes)
    monkeypatch.setattr(PsutilProcessProvider, "alive", lambda *_args: True)
    provider = PsutilProcessProvider(owner_uid=uid)

    def observe():
        return detect_spec(
            app=resolved.name,
            data_dir=resolved.data_dir,
            matcher=matcher_from_canonical(resolved.matcher.model_dump()),
            provider=provider,
        )

    assert observe().observation is Observation.RUNNING
    assert observe().pid == 42
    processes.pop(0)
    assert observe().observation is Observation.STOPPED
    processes.append(process(43, target_uid))
    assert observe().observation is Observation.RUNNING
    assert observe().pid == 43


def test_offline_check_rejects_unresolved_identity(application, valid_config):
    config = JobConfig.model_validate(valid_config)
    config = config.model_copy(
        update={
            "application": application,
            "consistency": config.consistency.model_copy(update={"mode": "application_offline"}),
        }
    )
    with pytest.raises(SourceDiscoveryError, match="resolved application identity"):
        require_application_offline(config, application=None)


def test_trigger_loader_uses_execution_account_not_file_location(
    application, valid_config, tmp_path
):
    valid_config["application"] = application.model_dump(exclude_none=True)
    valid_config["trigger"] = {"enabled": True}
    path = tmp_path / "unrelated-account" / "app.yaml"
    path.parent.mkdir()
    path.write_text(json.dumps(valid_config))
    instance = load_config(path).instances[0]
    assert instance.owner_uid == os.geteuid()
    assert instance.execution_policy["application"]["user"] == pwd.getpwuid(os.geteuid()).pw_name


def test_trigger_cli_reports_unsupported_user_without_traceback(
    application, valid_config, tmp_path, monkeypatch, capsys
):
    from vaultkeep.cli import main

    owner = pwd.getpwuid(os.geteuid())
    monkeypatch.setattr("vaultkeep.application.os.geteuid", lambda: owner.pw_uid or 1234)
    valid_config["application"] = application.model_dump(exclude_none=True) | {
        "user": owner.pw_name
    }
    valid_config["trigger"] = {"enabled": True}
    path = tmp_path / "app.yaml"
    path.write_text(json.dumps(valid_config))

    assert main(["--user", "--config", str(path), "trigger", "install"]) == 2
    error = capsys.readouterr().err
    assert str(path) in error
    assert "application.user" in error and "remove application.user" in error
    assert "Traceback" not in error
