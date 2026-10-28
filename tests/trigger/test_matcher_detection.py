from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import psutil
import pytest

from tests.trigger.conftest import FakeProvider, make_matcher
from vaultkeep import application as process_module
from vaultkeep.application import (
    Entrypoint,
    MatcherSpec,
    Observation,
    ProcessInfo,
    PsutilProcessProvider,
    detect_spec,
    match_identity,
)


@pytest.mark.parametrize("value", ["~/.electrum", ".electrum"])
@pytest.mark.parametrize("source", ["env", "argv"])
def test_directory_uses_process_owner_home_and_cwd(tmp_path, monkeypatch, value, source):
    owner_uid = 4242
    owner_home = tmp_path / "owner"
    owner_home.mkdir()
    executor_home = tmp_path / "executor"
    executor_home.mkdir()
    monkeypatch.setenv("HOME", str(executor_home))
    monkeypatch.chdir(executor_home)

    def account(uid):
        assert uid == owner_uid
        return SimpleNamespace(pw_dir=str(owner_home))

    monkeypatch.setattr(process_module.pwd, "getpwuid", account)
    live = SimpleNamespace(
        environ=lambda: {"ELECTRUMDIR": value} if source == "env" else {},
        cwd=lambda: str(owner_home),
        is_running=lambda: True,
        create_time=lambda: 1.0,
        uids=lambda: SimpleNamespace(real=owner_uid),
    )
    monkeypatch.setattr(process_module.psutil, "Process", lambda pid: live)
    provider = PsutilProcessProvider(owner_uid=owner_uid)
    provider._last = {100: 1.0}
    cmdline = ("/opt/app/run_app", "--dir", value) if source == "argv" else ("/opt/app/run_app",)
    monkeypatch.setattr(provider, "snapshot", lambda: [_proc(cmdline=cmdline)])
    result = detect_spec(
        app="electrum",
        data_dir=str(owner_home / ".electrum"),
        matcher=make_matcher(),
        provider=provider,
    )
    assert result.observation is Observation.RUNNING


@pytest.mark.parametrize("failure", ["cwd", "home", "identity"])
def test_unavailable_process_directory_context_never_reports_stopped(
    tmp_path, monkeypatch, failure
):
    provider = PsutilProcessProvider(owner_uid=4242)
    provider._last = {100: 1.0}
    value = "~/.electrum" if failure == "home" else ".electrum"
    monkeypatch.setattr(provider, "snapshot", lambda: [_proc(cmdline=("/opt/app/run_app",))])
    monkeypatch.setattr(provider, "alive", lambda *args: True)
    monkeypatch.setattr(provider, "read_env", lambda *args: {"ELECTRUMDIR": value})

    def account(uid):
        raise KeyError(uid)

    def cwd():
        if failure == "identity":
            monkeypatch.setattr(provider, "alive", lambda *args: False)
            return str(tmp_path)
        raise psutil.AccessDenied(100)

    monkeypatch.setattr(process_module.pwd, "getpwuid", account)
    monkeypatch.setattr(process_module.psutil, "Process", lambda pid: SimpleNamespace(cwd=cwd))
    result = detect_spec(
        app="electrum",
        data_dir=str(tmp_path / ".electrum"),
        matcher=make_matcher(),
        provider=provider,
    )
    assert result.observation is Observation.INSPECTION_UNAVAILABLE


def _proc(
    *,
    pid: int = 100,
    create_time: float = 1.0,
    exe: str | None = "/usr/bin/python3",
    cmdline: tuple[str, ...] = ("/opt/app/run_app", "--dir", "/home/user/.app"),
    env: dict[str, str] | None = None,
) -> ProcessInfo:
    return ProcessInfo(pid=pid, create_time=create_time, exe_realpath=exe, cmdline=cmdline, env=env)


def test_script_entrypoint_matches() -> None:
    result = match_identity(make_matcher(), _proc())
    assert result.matched


def test_exe_realpath_mismatch() -> None:
    matcher = MatcherSpec(exe_realpath="/opt/app/real")
    assert not match_identity(matcher, _proc(exe="/opt/app/other")).matched


def test_jar_entrypoint_matches() -> None:
    matcher = MatcherSpec(entrypoints=(Entrypoint(kind="jar", value="/opt/app/app.jar"),))
    proc = _proc(cmdline=("java", "-jar", "/opt/app/app.jar"))
    assert match_identity(matcher, proc).matched


def test_java_main_entrypoint_matches() -> None:
    matcher = MatcherSpec(entrypoints=(Entrypoint(kind="java_main", value="app.Main"),))
    proc = _proc(cmdline=("java", "-cp", "/opt/x.jar", "app.Main"))
    assert match_identity(matcher, proc).matched


def test_alternative_entrypoints_match_either_identity() -> None:
    matcher = MatcherSpec(
        entrypoints=(
            Entrypoint(kind="java_main", value="bisq.desktop.app.BisqAppMain"),
            Entrypoint(kind="jar", value="/opt/bisq/daemon.jar"),
        )
    )

    gui = _proc(cmdline=("java", "-cp", "/opt/bisq/desktop.jar", "bisq.desktop.app.BisqAppMain"))
    daemon = _proc(cmdline=("java", "-jar", "/opt/bisq/daemon.jar"))
    unrelated = _proc(cmdline=("java", "-jar", "/opt/other/app.jar"))

    assert match_identity(matcher, gui).matched
    assert match_identity(matcher, daemon).matched
    assert not match_identity(matcher, unrelated).matched


def test_argv_tokens_and_regex() -> None:
    matcher = MatcherSpec(
        entrypoints=(Entrypoint(kind="script", value="/opt/app/run_app"),),
        argv_tokens=("--dir",),
        cmdline_regex=r"--dir|/opt/.*",
    )
    assert match_identity(matcher, _proc()).matched


def test_env_eq_requires_env() -> None:
    matcher = make_matcher(env_key="ELECTRUMDIR", env_value="/home/user/.app")
    assert not match_identity(matcher, _proc(env=None)).matched
    assert match_identity(matcher, _proc(env={"ELECTRUMDIR": "/home/user/.app"})).matched


def test_detect_running_via_env() -> None:
    proc = _proc(cmdline=("/opt/app/run_app",))
    provider = FakeProvider([proc], env={100: {"ELECTRUMDIR": "/home/user/.app"}})
    result = detect_spec(
        app="electrum",
        data_dir="/home/user/.app",
        matcher=make_matcher(),
        provider=provider,
    )
    assert result.observation is Observation.RUNNING


def test_detect_running_via_argv() -> None:
    provider = FakeProvider([_proc()])
    result = detect_spec(
        app="electrum",
        data_dir="/home/user/.app",
        matcher=make_matcher(),
        provider=provider,
    )
    assert result.observation is Observation.RUNNING


def test_detect_ambiguous_when_dir_unresolved() -> None:
    proc = _proc(cmdline=("/opt/app/run_app",))
    provider = FakeProvider([proc])
    result = detect_spec(
        app="electrum",
        data_dir="/home/user/.app",
        matcher=make_matcher(),
        provider=provider,
    )
    assert result.observation is Observation.AMBIGUOUS


def test_detect_stopped_when_dir_differs() -> None:
    provider = FakeProvider([_proc()], env={100: {"ELECTRUMDIR": "/home/other/.app"}})
    result = detect_spec(
        app="electrum",
        data_dir="/home/user/.app",
        matcher=make_matcher(),
        provider=provider,
    )
    assert result.observation is Observation.STOPPED


def test_detect_inspection_unavailable_when_env_unreadable() -> None:
    proc = _proc(cmdline=("/opt/app/run_app",))
    provider = FakeProvider([proc], env_readable=False)
    result = detect_spec(
        app="electrum",
        data_dir="/home/user/.app",
        matcher=make_matcher(),
        provider=provider,
    )
    assert result.observation is Observation.INSPECTION_UNAVAILABLE


def test_detect_stopped_when_no_process() -> None:
    provider = FakeProvider([])
    result = detect_spec(
        app="electrum",
        data_dir="/home/user/.app",
        matcher=make_matcher(),
        provider=provider,
    )
    assert result.observation is Observation.STOPPED


def test_unreadable_executable_is_unavailable_not_stopped() -> None:
    result = detect_spec(
        app="wasabi",
        data_dir="/home/user/.app",
        matcher=MatcherSpec(exe_realpath="/opt/app/real"),
        provider=FakeProvider([_proc(exe=None)]),
    )
    assert result.observation is Observation.INSPECTION_UNAVAILABLE


def test_executable_with_entrypoint_excludes_unrelated_hardened_process() -> None:
    matcher = MatcherSpec(
        exe_realpath="/opt/wasabi/WalletWasabi",
        entrypoints=(Entrypoint("script", "/opt/wasabi/WalletWasabi"),),
    )
    provider = FakeProvider([_proc(exe=None, cmdline=("/opt/electrum/run_electrum",))])
    result = detect_spec(
        app="wasabi", data_dir="/home/user/.app", matcher=matcher, provider=provider
    )
    assert result.observation is Observation.STOPPED
    provider.processes.append(
        _proc(
            pid=101,
            exe="/opt/wasabi/WalletWasabi",
            cmdline=("/opt/wasabi/WalletWasabi", "--datadir", "/home/user/.app"),
        )
    )
    assert (
        detect_spec(
            app="wasabi", data_dir="/home/user/.app", matcher=matcher, provider=provider
        ).observation
        is Observation.RUNNING
    )


def test_unreadable_command_line_is_unavailable_not_stopped() -> None:
    result = detect_spec(
        app="electrum",
        data_dir="/home/user/.app",
        matcher=make_matcher(),
        provider=FakeProvider([replace(_proc(), cmdline=(), cmdline_available=False)]),
    )
    assert result.observation is Observation.INSPECTION_UNAVAILABLE


def test_unrelated_process_does_not_require_environment_inspection(monkeypatch) -> None:
    provider = FakeProvider([_proc(cmdline=("/opt/other/app",))], env_readable=False)

    def unexpected_read(*_args):
        raise AssertionError("unrelated environment inspected")

    monkeypatch.setattr(provider, "read_env", unexpected_read)
    result = detect_spec(
        app="electrum",
        data_dir="/home/user/.app",
        matcher=make_matcher(env_key="ELECTRUMDIR", env_value="/home/user/.app"),
        provider=provider,
    )
    assert result.observation is Observation.STOPPED


def test_snapshot_access_failure_blocks_detection(monkeypatch) -> None:
    provider = FakeProvider([])

    def unavailable():
        raise psutil.AccessDenied(42)

    monkeypatch.setattr(provider, "snapshot", unavailable)
    assert (
        detect_spec(
            app="electrum", data_dir="/home/user/.app", matcher=make_matcher(), provider=provider
        ).observation
        is Observation.INSPECTION_UNAVAILABLE
    )


def test_matching_process_replacement_requires_a_fresh_snapshot() -> None:
    # Use Bisq's directory token so the lifetime check is reached without an env read.
    provider = FakeProvider(
        [_proc(cmdline=("/opt/app/run_app", "--appDataDir", "/home/user/.app"))], alive=False
    )
    result = detect_spec(
        app="bisq", data_dir="/home/user/.app", matcher=make_matcher(), provider=provider
    )
    assert result.observation is Observation.INSPECTION_UNAVAILABLE
    provider.processes = []
    assert (
        detect_spec(
            app="bisq", data_dir="/home/user/.app", matcher=make_matcher(), provider=provider
        ).observation
        is Observation.STOPPED
    )


def test_bisq_accepts_separate_and_equals_app_data_dir_arguments() -> None:
    matcher = MatcherSpec(
        entrypoints=(
            Entrypoint(kind="java_main", value="bisq.desktop.app.BisqAppMain"),
            Entrypoint(kind="jar", value="/opt/bisq/daemon.jar"),
        )
    )
    processes = [
        _proc(
            pid=100,
            cmdline=(
                "java",
                "-cp",
                "/opt/bisq/desktop.jar",
                "bisq.desktop.app.BisqAppMain",
                "--appDataDir",
                "/home/user/bisq-gui",
            ),
        ),
        _proc(
            pid=101,
            cmdline=(
                "java",
                "-jar",
                "/opt/bisq/daemon.jar",
                "--appDataDir=/home/user/bisq-daemon",
            ),
        ),
    ]
    provider = FakeProvider(processes)

    for data_dir in ("/home/user/bisq-gui", "/home/user/bisq-daemon"):
        assert (
            detect_spec(
                app="bisq",
                data_dir=data_dir,
                matcher=matcher,
                provider=provider,
            ).observation
            is Observation.RUNNING
        )


def test_bisq_triggers_for_target_instance_while_another_is_running() -> None:
    matcher = MatcherSpec(
        entrypoints=(
            Entrypoint(kind="java_main", value="bisq.desktop.app.BisqAppMain"),
            Entrypoint(kind="jar", value="/opt/bisq/daemon.jar"),
        )
    )
    target = _proc(
        pid=100,
        cmdline=(
            "java",
            "-cp",
            "/opt/bisq/desktop.jar",
            "bisq.desktop.app.BisqAppMain",
            "--appDataDir=/data/bisq/Bisq_Alice",
        ),
    )
    other = _proc(
        pid=101,
        cmdline=(
            "java",
            "-jar",
            "/opt/bisq/daemon.jar",
            "--appDataDir=/data/bisq/Bisq_Bob",
        ),
    )
    provider = FakeProvider([target, other])

    assert (
        detect_spec(
            app="bisq",
            data_dir="/data/bisq/Bisq_Alice",
            matcher=matcher,
            provider=provider,
        ).observation
        is Observation.RUNNING
    )

    provider.processes.remove(target)
    assert (
        detect_spec(
            app="bisq",
            data_dir="/data/bisq/Bisq_Alice",
            matcher=matcher,
            provider=provider,
        ).observation
        is Observation.STOPPED
    )


def test_bisq_does_not_use_non_bisq_directory_option_spellings() -> None:
    result = detect_spec(
        app="bisq",
        data_dir="/home/user/.app",
        matcher=make_matcher(),
        provider=FakeProvider(
            [_proc(cmdline=("/opt/app/run_app", "--app-data-dir", "/home/user/.app"))]
        ),
    )

    assert result.observation is Observation.AMBIGUOUS
