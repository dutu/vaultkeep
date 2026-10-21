from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.trigger.conftest import (
    FakeController,
    FakeProvider,
    FakeRunner,
    RecordingNotifier,
    child_result,
    make_config,
    make_instance,
)
from tests.trigger.test_monitor import _running_proc
from vaultkeep.trigger.errors import BlockedError, ConfigurationError
from vaultkeep.trigger.monitor import Monitor
from vaultkeep.trigger.preflight import runtime_preflight, validate_jobs
from vaultkeep.trigger.state import StateStore


def _runtime_config(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    binary = tmp_path / "vaultkeep"
    binary.write_text('#!/bin/sh\nif [ "$1" = --version ]; then printf "0.1.0\\n"; fi\n')
    binary.chmod(0o700)
    instance = make_instance(confirm_seconds=0)
    return replace(make_config(instance), vaultkeep=binary), runtime


def test_runtime_preflight_is_read_only_and_checks_full_jobs_and_version(
    tmp_path: Path, monkeypatch
) -> None:
    config, runtime = _runtime_config(tmp_path, monkeypatch)
    config = replace(
        config,
        instances=(
            replace(
                config.instances[0],
                job=replace(config.instances[0].job, params={"NETWORK": "mainnet"}),
            ),
        ),
    )
    runner = FakeRunner([child_result(), child_result(stdout_tail="0.1.0")])
    root = tmp_path / "absent" / "state"
    assert runtime_preflight(config, FakeController(), root, runner=runner) == "0.1.0"
    assert not root.parent.exists() and not (runtime / "vaultkeep-trigger").exists()
    assert runner.calls[0][-2:] == ["validate", "--backup-only"]
    assert "--schema-only" not in runner.calls[0]
    assert "NETWORK=mainnet" in runner.calls[0]
    assert runner.calls[1] == [str(config.vaultkeep), "--version"]


def test_backup_validation_skips_disabled_jobs(tmp_path: Path, monkeypatch) -> None:
    config, _runtime = _runtime_config(tmp_path, monkeypatch)
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    runner = FakeRunner([child_result()])
    config = replace(
        config, instances=(*config.instances, make_instance(instance_id="disabled", enabled=False))
    )
    validate_jobs(config, runner=runner)
    assert len(runner.calls) == 1 and runner.calls[0][-1] == "--backup-only"


def test_template_failure_blocks_preflight_before_job_commands(tmp_path: Path, monkeypatch) -> None:
    config, _runtime = _runtime_config(tmp_path, monkeypatch)
    controller = FakeController()
    monkeypatch.setattr(controller, "template_ready", lambda: False)
    with pytest.raises(BlockedError, match="worker template"):
        runtime_preflight(config, controller, tmp_path / "state", runner=FakeRunner([]))


def test_state_parent_is_checked_without_a_write_probe(tmp_path: Path, monkeypatch) -> None:
    config, _runtime = _runtime_config(tmp_path, monkeypatch)
    parent = tmp_path / "not-a-directory"
    parent.write_text("preserve")
    with pytest.raises(BlockedError, match="not writable"):
        runtime_preflight(config, FakeController(), parent / "state", runner=FakeRunner([]))
    assert parent.read_text() == "preserve"


def test_runtime_version_mismatch_warns_without_rejecting(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    config, _runtime = _runtime_config(tmp_path, monkeypatch)
    runner = FakeRunner([child_result(), child_result(stdout_tail="0.3.0")])
    assert runtime_preflight(config, FakeController(), tmp_path / "state", runner=runner) == "0.3.0"
    assert "differs from this installation" in caplog.text


def test_monitor_preflight_rejects_bad_job_without_state_and_releases_lock(
    tmp_path: Path, monkeypatch
) -> None:
    config, runtime = _runtime_config(tmp_path, monkeypatch)
    config.vaultkeep.write_text("#!/bin/sh\nexit 3\n")
    store = StateStore(tmp_path / "state")
    monitor = Monitor(
        config,
        store=store,
        provider=FakeProvider([]),
        controller=FakeController(),
        notifier=RecordingNotifier(),
    )
    with pytest.raises(ConfigurationError, match="VaultKeep exit 3"):
        monitor.run()
    assert not store.root.exists()
    from vaultkeep.trigger.monitor import MonitorLock

    with MonitorLock(runtime / "vaultkeep-trigger" / "monitor.lock"):
        pass


def test_second_monitor_refuses_before_vaultkeep_preflight(tmp_path: Path, monkeypatch) -> None:
    from vaultkeep.trigger.monitor import MonitorLock

    config, runtime = _runtime_config(tmp_path, monkeypatch)
    store = StateStore(tmp_path / "state")
    monitor = Monitor(
        config,
        store=store,
        provider=FakeProvider([]),
        controller=FakeController(),
        notifier=RecordingNotifier(),
    )

    def unexpected_preflight(*_args, **_kwargs):
        raise AssertionError("duplicate monitor ran full preflight")

    monkeypatch.setattr("vaultkeep.trigger.preflight.runtime_preflight", unexpected_preflight)
    with MonitorLock(runtime / "vaultkeep-trigger" / "monitor.lock"):
        with pytest.raises(BlockedError, match="another monitor"):
            monitor.run()
    assert not store.root.exists()


def test_monitor_records_runtime_baseline_in_requests(tmp_path: Path, monkeypatch) -> None:
    config, _runtime = _runtime_config(tmp_path, monkeypatch)
    store = StateStore(tmp_path / "state")
    provider = FakeProvider([_running_proc()])
    monitor = Monitor(
        config,
        store=store,
        provider=provider,
        controller=FakeController(),
        notifier=RecordingNotifier(),
    )
    monitor._sleep = lambda _seconds: monitor.request_stop()
    monitor.run()
    provider.processes = []
    monitor.run_once()
    monitor.run_once()
    state = store.read_instance(config.instances[0].id)
    assert state is not None and state.active_attempt_id is not None
    request = store.read_request(state.active_attempt_id)
    assert request is not None and request.preflight_version == "0.1.0"
    assert request.environment["XDG_RUNTIME_DIR"] == str(_runtime)
