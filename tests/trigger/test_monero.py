from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from tests.helpers.trigger import FakeProvider, FakeRunner, make_instance
from vaultkeep import application as application_module
from vaultkeep.application import (
    Entrypoint,
    MatcherSpec,
    Observation,
    ProcessInfo,
    PsutilProcessProvider,
    require_application_offline,
    resolve_application,
)
from vaultkeep.config.models import ApplicationConfig, JobConfig
from vaultkeep.errors import SourceDiscoveryError
from vaultkeep.trigger.detection import detect
from vaultkeep.trigger.executor import exec_worker
from vaultkeep.trigger.lifecycle import ExecutionContext, progress
from vaultkeep.trigger.models import AttemptOutcome, InstanceState

GUI = "/opt/monero-gui/monero-wallet-gui"
RPC = "/opt/monero-cli/monero-wallet-rpc"
MATCHER = MatcherSpec(entrypoints=(Entrypoint("script", GUI), Entrypoint("script", RPC)))
MARKER = "monero-storage/.portable"
PORTABLE = "monero-storage/settings.ini"
NORMAL = "monero-project/monero-core.conf"


class MoneroProvider(FakeProvider):
    def __init__(self, cwd):
        super().__init__()
        self.cwd = cwd
        self.files = {}

    def resolve_directory(self, pid, value):
        return str((self.cwd / value).resolve())

    def read_text_file(self, pid, path):
        return self.files.get(Path(path), (None, "missing"))


def process(path, *args, pid=1):
    return ProcessInfo(pid, 1.0, path, (path, *args))


@pytest.fixture
def watch(tmp_path):
    target = tmp_path / "wallets"
    target.mkdir()
    instance = make_instance(app="monero", data_dir=str(target), matcher=MATCHER)
    return instance, MoneroProvider(tmp_path)


@pytest.fixture
def nested_wallet(tmp_path):
    wallet_file = tmp_path / "Monero" / "wallets" / "my-wallet" / "m-wallet"
    wallet_file.parent.mkdir(parents=True)
    wallet_file.touch()
    return wallet_file


@pytest.mark.parametrize("frontend", [GUI, RPC])
def test_nested_wallet_matches_its_containing_directory(tmp_path, nested_wallet, frontend):
    wallet_dir = nested_wallet.parent
    instance = make_instance(app="monero", data_dir=str(wallet_dir), matcher=MATCHER)
    provider = MoneroProvider(tmp_path)
    if frontend == GUI:
        provider.processes = [process(GUI)]
        provider.env[1] = {"XDG_CONFIG_HOME": str(tmp_path / ".config")}
        provider.files[tmp_path / ".config" / NORMAL] = (
            f"wallet_path={nested_wallet}",
            "resolved",
        )
    else:
        provider.processes = [process(RPC, "--wallet-file", str(nested_wallet))]

    assert detect(instance, provider).observation is Observation.RUNNING
    outer = replace(instance, data_dir=wallet_dir.parent)
    assert detect(outer, provider).observation is Observation.AMBIGUOUS


@pytest.mark.parametrize("mode", ["normal", "legacy_portable", "marked_portable"])
@pytest.mark.parametrize("use_xdg", [True, False])
def test_gui_resolves_process_settings_and_qt_path(watch, mode, use_xdg):
    instance, provider = watch
    # argv[0] names the absolute frontend even if /proc/PID/exe is hidden.
    provider.processes = [replace(process(GUI), exe_realpath=None)]
    home = provider.cwd / "process-home"
    xdg = provider.cwd / "process-xdg"
    provider.env[1] = {"HOME": str(home), "XDG_CONFIG_HOME": str(xdg) if use_xdg else ""}
    normal = (xdg if use_xdg else home / ".config") / NORMAL
    text = "[General]\n" + r'wallet_path="wallets/A, \xe9 \xd83d\xde00.keys"' + "\n"
    if mode == "normal":
        provider.files[normal] = (text, "resolved")
    else:
        provider.files[provider.cwd / PORTABLE] = (text, "resolved")
        provider.files[normal] = ("wallet_path=elsewhere/wallet", "resolved")
    if mode == "marked_portable":
        provider.files[provider.cwd / MARKER] = ("portable\n", "resolved")
    assert detect(instance, provider).observation is Observation.RUNNING


def test_disabled_marker_ignores_stale_portable_wallet(watch):
    instance, provider = watch
    xdg = provider.cwd / "xdg"
    provider.processes = [process(GUI)]
    provider.env[1] = {"XDG_CONFIG_HOME": str(xdg)}
    provider.files[provider.cwd / MARKER] = (" disabled\r\n", "resolved")
    provider.files[provider.cwd / PORTABLE] = ("wallet_path=stale/a", "resolved")
    provider.files[xdg / NORMAL] = ("wallet_path=wallets/a", "resolved")

    assert detect(instance, provider).observation is Observation.RUNNING
    stale = replace(instance, data_dir=provider.cwd / "stale")
    assert detect(stale, provider).observation is Observation.AMBIGUOUS


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        (("portable\n", "resolved"), Observation.AMBIGUOUS),
        ((None, "inaccessible"), Observation.INSPECTION_UNAVAILABLE),
    ],
)
def test_marker_without_readable_portable_settings_does_not_fall_back(watch, marker, expected):
    instance, provider = watch
    xdg = provider.cwd / "xdg"
    provider.processes = [process(GUI)]
    provider.env[1] = {"XDG_CONFIG_HOME": str(xdg)}
    provider.files[provider.cwd / MARKER] = marker
    provider.files[xdg / NORMAL] = ("wallet_path=wallets/a", "resolved")
    assert detect(instance, provider).observation is expected


@pytest.mark.parametrize(
    ("args", "settings", "expected"),
    [
        (("--wallet-file", "wallets/a"), None, Observation.RUNNING),
        (("--config-file=rpc.conf",), "wallet-file=wallets/a", Observation.RUNNING),
        (
            ("--config-file", "rpc.conf", "--wallet-file=wallets/../wallets/a"),
            "wallet-file=wallets/a",
            Observation.RUNNING,
        ),
        (
            ("--config-file", "rpc.conf", "--wallet-file=wallets/a"),
            "wallet-file=other/b",
            Observation.RUNNING,
        ),
        (
            ("--config-file", "rpc.conf", "--wallet-file=other/b"),
            "wallet-file=wallets/a",
            Observation.AMBIGUOUS,
        ),
        (("--wallet-file=",), None, Observation.AMBIGUOUS),
        (("--wallet-file=wallets/a", "--wallet-file=wallets/b"), None, Observation.AMBIGUOUS),
        (("--wallet-file=wallets/a", "--wallet-dir=wallets"), None, Observation.AMBIGUOUS),
        (("--wallet-file=wallets/nested/a",), None, Observation.AMBIGUOUS),
        (
            ("--config-file", "rpc.conf", "--wallet-file=wallets/a"),
            "wallet-file=wallets/a\nwallet-dir=wallets",
            Observation.AMBIGUOUS,
        ),
        (("--config-file", "rpc.conf"), None, Observation.AMBIGUOUS),
    ],
)
def test_rpc_selection(watch, args, settings, expected):
    instance, provider = watch
    # A readable native executable identifies RPC even if argv[0] is a bare name.
    provider.processes = [replace(process(RPC), cmdline=("monero-wallet-rpc", *args))]
    if settings is not None:
        provider.files[provider.cwd / "rpc.conf"] = (settings, "resolved")
    assert detect(instance, provider).observation is expected


def test_only_executable_or_argv_zero_establishes_identity(watch):
    instance, provider = watch
    provider.processes = [replace(process("/usr/bin/unrelated", GUI, RPC), exe_realpath=None)]
    assert detect(instance, provider).observation is Observation.STOPPED
    provider.processes = [replace(process(RPC), exe_realpath=None, cmdline=("monero-wallet-rpc",))]
    assert detect(instance, provider).observation is Observation.INSPECTION_UNAVAILABLE
    provider.processes = [replace(process(RPC), exe_realpath=RPC + " (deleted)", cmdline=())]
    assert detect(instance, provider).observation is Observation.INSPECTION_UNAVAILABLE
    provider.processes = [replace(process(RPC), cmdline=(), cmdline_available=False)]
    assert detect(instance, provider).observation is Observation.INSPECTION_UNAVAILABLE


def test_unreadable_or_malformed_portable_settings_do_not_fall_back(watch):
    instance, provider = watch
    provider.processes = [process(GUI)]
    provider.env[1] = {"HOME": str(provider.cwd)}
    provider.files[provider.cwd / ".config" / NORMAL] = ("wallet_path=wallets/a", "resolved")
    portable = provider.cwd / PORTABLE
    provider.files[portable] = (None, "inaccessible")
    assert detect(instance, provider).observation is Observation.INSPECTION_UNAVAILABLE
    provider.files[portable] = ('wallet_path="unfinished', "resolved")
    assert detect(instance, provider).observation is Observation.AMBIGUOUS


@pytest.mark.parametrize("first", [GUI, RPC])
def test_all_frontends_must_exit_before_confirmation_and_worker_rechecks(watch, store, first):
    instance, provider = watch
    gui = process(GUI)
    rpc = process(RPC, "--wallet-file=wallets/a", pid=2)
    provider.processes = [gui if first == GUI else rpc]
    portable = provider.cwd / PORTABLE
    provider.files[portable] = ("wallet_path=wallets/a", "resolved")
    context = ExecutionContext(vaultkeep="/usr/local/bin/vaultkeep", environment={})
    state = InstanceState(instance_id=instance.id)

    def poll(now):
        return progress(state, instance, detect(instance, provider), context, monotonic_now=now)

    assert [event.kind for event in poll(0)] == ["armed"]
    provider.processes = [gui, rpc]
    provider.files[portable] = ("wallet_path=other/a", "resolved")
    assert detect(instance, provider).observation is Observation.RUNNING
    provider.processes = [gui]
    assert detect(instance, provider).observation is Observation.AMBIGUOUS
    assert not poll(10)
    assert state.armed and state.candidate_since is None
    del provider.files[portable]
    assert not poll(20)
    assert state.armed and state.candidate_since is None
    provider.processes = []
    assert [event.kind for event in poll(21)] == ["candidate_started"]
    assert [event.kind for event in poll(26)] == ["request_confirmed"]

    assert state.pending_request is not None
    pending = state.pending_request
    store.write_request(pending)
    provider.processes = [replace(rpc, cmdline=(RPC, "--wallet-file=other/a"))]
    result = exec_worker(pending.attempt_id, store=store, provider=provider, runner=FakeRunner([]))
    assert result.outcome is AttemptOutcome.BLOCKED
    assert "another configured Monero wallet process" in result.message


@pytest.mark.parametrize(
    "change",
    [
        {"entrypoints": [{"kind": "script", "value": GUI}]},
        {"entrypoints": [{"kind": "java_main", "value": GUI}, {"kind": "script", "value": RPC}]},
        {"exe_realpath": GUI},
        {"argv_tokens": ["--wallet-file"]},
        {"cmdline_regex": "monero"},
        {"env_eq": {"HOME": "/home/user"}},
    ],
)
def test_matcher_requires_both_roles_and_full_guard(change):
    with pytest.raises(ValueError, match=r"Monero matcher|Monero requires"):
        ApplicationConfig.model_validate(
            {"name": "monero", "data_dir": "/wallets", "matcher": MATCHER.canonical() | change}
        )


def test_manual_offline_guard_uses_same_monero_detection(watch, valid_config, monkeypatch):
    instance, provider = watch
    valid_config["consistency"] = {"mode": "application_offline"}
    valid_config["application"] = {
        "name": "monero",
        "data_dir": str(instance.data_dir),
        "matcher": MATCHER.canonical(),
    }
    config = JobConfig.model_validate(valid_config)
    app = resolve_application(config.application, user_mode=True)
    monkeypatch.setattr(application_module, "PsutilProcessProvider", lambda **kwargs: provider)
    require_application_offline(config, application=app)
    provider.processes = [process(RPC, "--wallet-file=other/a")]
    with pytest.raises(SourceDiscoveryError, match="another configured Monero wallet process"):
        require_application_offline(config, application=app)


def test_bounded_settings_read(tmp_path):
    provider = PsutilProcessProvider()
    pid = os.getpid()
    provider._last[pid] = psutil.Process(pid).create_time()
    path = tmp_path / "settings.ini"
    assert provider.read_text_file(pid, str(path)) == (None, "missing")
    path.write_bytes(b"\xef\xbb\xbfwallet_path=a")
    assert provider.read_text_file(pid, str(path)) == ("wallet_path=a", "resolved")
    path.write_bytes(b"\xff")
    assert provider.read_text_file(pid, str(path)) == (None, "unresolved")
    with path.open("wb") as stream:
        stream.truncate(1024 * 1024 + 1)
    assert provider.read_text_file(pid, str(path)) == (None, "unresolved")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    assert provider.read_text_file(pid, str(fifo)) == (None, "unresolved")
