"""Shared fixtures and fakes for the Trigger test suite."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from vaultkeep.trigger.childproc import ChildResult
from vaultkeep.trigger.config import (
    Entrypoint,
    InstanceConfig,
    JobSpec,
    MatcherSpec,
    NotificationConfig,
    TriggerConfig,
)
from vaultkeep.trigger.matcher import ProcessInfo
from vaultkeep.trigger.models import Request, new_uuid, utc_now
from vaultkeep.trigger.state import StateStore
from vaultkeep.trigger.systemd import SubmissionClass, SubmissionResult, UnitState


def child_result(
    *,
    exit_code: int | None = 0,
    stdout_tail: str | None = None,
    diagnostics: bytes = b"",
    spawn_error: str | None = None,
    signal_number: int | None = None,
) -> ChildResult:
    return ChildResult(
        exit_code=exit_code,
        signal_number=signal_number,
        stdout_tail=stdout_tail,
        diagnostics=diagnostics,
        diagnostics_truncated=False,
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
        duration_seconds=1.0,
        spawn_error=spawn_error,
    )


class FakeRunner:
    """Deterministic child runner that returns pre-seeded results in order."""

    def __init__(self, results: list[ChildResult]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.timeouts: list[float | None] = []

    def run(self, argv, *, env, timeout=None) -> ChildResult:
        self.calls.append(list(argv))
        self.environments.append(dict(env))
        self.timeouts.append(timeout)
        if not self.results:
            raise AssertionError(f"unexpected child invocation: {argv}")
        return self.results.pop(0)


class FakeProvider:
    """Deterministic process provider for tests."""

    def __init__(
        self,
        processes: list[ProcessInfo] | None = None,
        env: dict[int, dict[str, str]] | None = None,
        alive: bool = True,
        env_readable: bool = True,
    ) -> None:
        self.processes = processes or []
        self.env = env or {}
        self.alive_result = alive
        self.env_readable = env_readable

    def snapshot(self) -> list[ProcessInfo]:
        return list(self.processes)

    def read_env(self, pid: int, keys) -> dict[str, str] | None:
        if not self.env_readable:
            return None
        return {key: self.env.get(pid, {})[key] for key in keys if key in self.env.get(pid, {})}

    def alive(self, pid: int, create_time: float) -> bool:
        return self.alive_result and any(
            proc.pid == pid and proc.create_time == create_time for proc in self.processes
        )


class FakeController:
    """Deterministic systemd controller for tests."""

    def __init__(
        self,
        *,
        submission: SubmissionResult | None = None,
        reachable: bool = True,
    ) -> None:
        self.submission = submission or SubmissionResult(SubmissionClass.ACCEPTED, 0, "", True)
        self.reachable = reachable
        self.started: list[str] = []
        self.resets: list[str] = []
        self.states: dict[str, UnitState] = {}

    def start_unit(self, unit: str) -> SubmissionResult:
        self.started.append(unit)
        return self.submission

    def unit_state(self, unit: str) -> UnitState | None:
        return self.states.get(unit)

    def manager_reachable(self) -> bool:
        return self.reachable

    @staticmethod
    def template_ready() -> bool:
        return True

    def reset_failed(self, unit: str) -> None:
        self.resets.append(unit)


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, body: str) -> None:
        self.calls.append((title, body))


def make_matcher(
    *,
    script: str = "/opt/app/run_app",
    env_key: str | None = None,
    env_value: str | None = None,
    argv_tokens: tuple[str, ...] = (),
) -> MatcherSpec:
    env_eq = {env_key: env_value} if env_key and env_value else {}
    return MatcherSpec(
        entrypoint=Entrypoint(kind="script", value=script),
        argv_tokens=argv_tokens,
        env_eq=env_eq,
    )


def make_instance(
    *,
    instance_id: str = "electrum-main",
    app: str = "electrum",
    data_dir: str = "/home/user/.electrum",
    enabled: bool = True,
    confirm_seconds: float = 5.0,
    deadline_seconds: float | None = None,
    matcher: MatcherSpec | None = None,
) -> InstanceConfig:
    return InstanceConfig(
        id=instance_id,
        app=app,
        enabled=enabled,
        data_dir=Path(data_dir),
        matcher=matcher or make_matcher(),
        job=JobSpec(config=Path("/jobs/app.yaml"), params={}),
        poll_seconds=1.0,
        confirm_seconds=confirm_seconds,
        deadline_seconds=deadline_seconds,
    )


def make_config(*instances: InstanceConfig) -> TriggerConfig:
    return TriggerConfig(
        poll_seconds=1.0,
        confirm_seconds=5.0,
        deadline_seconds=None,
        log_level="INFO",
        vaultkeep=Path("/usr/local/bin/vaultkeep"),
        notifications=NotificationConfig(),
        instances=tuple(instances),
        source_path=Path("/tmp/job.yaml"),
    )


def make_request(instance: InstanceConfig | None = None) -> Request:
    instance = instance or make_instance()
    now = utc_now()
    return Request(
        request_id=new_uuid(),
        attempt_id=new_uuid(),
        instance_id=instance.id,
        created_at=now,
        confirmed_at=now,
        boot_id=None,
        config_fingerprint=instance.fingerprint(),
        app=instance.app,
        data_dir=str(instance.data_dir),
        matcher=instance.matcher.canonical(),
        job_config=str(instance.job.config),
        job_params=dict(instance.job.params),
        vaultkeep="/usr/local/bin/vaultkeep",
        environment={"HOME": "/home/user"},
        preflight_version="0.1.0",
        deadline_seconds=instance.deadline_seconds,
    )


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    result = StateStore(tmp_path / "state")
    result.ensure()
    return result


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


def replace_request(request, **changes):
    return replace(request, **changes)
