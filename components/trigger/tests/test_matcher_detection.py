from __future__ import annotations

from dataclasses import replace

import psutil

from tests.conftest import FakeProvider, make_matcher
from vaultkeep_trigger.config import Entrypoint, MatcherSpec
from vaultkeep_trigger.detection import detect_spec
from vaultkeep_trigger.matcher import ProcessInfo, match_identity
from vaultkeep_trigger.models import Observation


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
    matcher = MatcherSpec(entrypoint=Entrypoint(kind="jar", value="/opt/app/app.jar"))
    proc = _proc(cmdline=("java", "-jar", "/opt/app/app.jar"))
    assert match_identity(matcher, proc).matched


def test_java_main_entrypoint_matches() -> None:
    matcher = MatcherSpec(entrypoint=Entrypoint(kind="java_main", value="app.Main"))
    proc = _proc(cmdline=("java", "-cp", "/opt/x.jar", "app.Main"))
    assert match_identity(matcher, proc).matched


def test_argv_tokens_and_regex() -> None:
    matcher = MatcherSpec(
        entrypoint=Entrypoint(kind="script", value="/opt/app/run_app"),
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
        entrypoint=Entrypoint("script", "/opt/wasabi/WalletWasabi"),
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
        [_proc(cmdline=("/opt/app/run_app", "--data-dir", "/home/user/.app"))], alive=False
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
