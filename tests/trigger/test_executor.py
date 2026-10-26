from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from tests.trigger.conftest import FakeProvider, FakeRunner, child_result
from vaultkeep.trigger.errors import IntegrationError, StateError
from vaultkeep.trigger.executor import exec_worker
from vaultkeep.trigger.matcher import ProcessInfo
from vaultkeep.trigger.models import AttemptOutcome, Receipt, Request, new_uuid, utc_now
from vaultkeep.trigger.state import StateStore

MATCHER = {
    "exe_realpath": None,
    "entrypoints": [{"kind": "script", "value": "/opt/app/run_app"}],
    "argv_tokens": [],
    "cmdline_regex": None,
    "env_eq": {},
}


def _bound_request() -> Request:
    attempt_id = new_uuid()
    now = utc_now()
    return Request(
        request_id=new_uuid(),
        attempt_id=attempt_id,
        instance_id="electrum-main",
        created_at=now,
        confirmed_at=now,
        boot_id="boot-1",
        config_fingerprint="fp",
        app="electrum",
        data_dir="/home/user/.app",
        matcher=MATCHER,
        job_config="/jobs/app.yaml",
        job_params={"NETWORK": "mainnet"},
        vaultkeep="/usr/local/bin/vaultkeep",
        environment={"HOME": "/home/user"},
        preflight_version=None,
        deadline_seconds=None,
        owner_uid=os.geteuid(),
        execution_policy={
            "consistency": "files",
            "owner_uid": os.geteuid(),
            "application": {
                "name": "electrum",
                "user": "test",
                "data_dir": "/home/user/.app",
                "matcher": MATCHER,
            },
        },
    )


def _running_provider() -> FakeProvider:
    proc = ProcessInfo(
        pid=42,
        create_time=1.0,
        exe_realpath="/usr/bin/python3",
        cmdline=("/opt/app/run_app", "--dir", "/home/user/.app"),
    )
    return FakeProvider([proc])


def test_created_receipt(store: StateStore) -> None:
    request = _bound_request()
    store.write_request(request)
    runner = FakeRunner(
        [
            child_result(exit_code=0, stdout_tail="0.1.0"),
            child_result(exit_code=0, stdout_tail="run: created"),
        ]
    )
    result = exec_worker(request.attempt_id, store=store, provider=FakeProvider([]), runner=runner)
    assert result.outcome is AttemptOutcome.CREATED
    receipt = store.read_receipt(request.attempt_id)
    assert receipt is not None
    assert receipt.outcome is AttemptOutcome.CREATED
    assert receipt.vaultkeep_version == "0.1.0"
    assert runner.calls[1][-1] == "run"
    assert runner.environments[0] == request.environment
    assert runner.environments[1] == request.environment | {
        "VAULTKEEP_EXPECTED_POLICY": json.dumps(request.execution_policy, sort_keys=True)
    }
    assert {p.name for p in store.attempt_dir(request.attempt_id).iterdir()} == {
        "request.json",
        "receipt.json",
    }


def test_unchanged_receipt(store: StateStore) -> None:
    request = _bound_request()
    store.write_request(request)
    runner = FakeRunner(
        [
            child_result(exit_code=0, stdout_tail="0.1.0"),
            child_result(exit_code=0, stdout_tail="run: unchanged"),
        ]
    )
    result = exec_worker(request.attempt_id, store=store, provider=FakeProvider([]), runner=runner)
    assert result.outcome is AttemptOutcome.UNCHANGED


def test_deferred_receipt(store: StateStore) -> None:
    request = _bound_request()
    store.write_request(request)
    runner = FakeRunner(
        [
            child_result(exit_code=0, stdout_tail="0.1.0"),
            child_result(exit_code=10, stdout_tail=None),
        ]
    )
    result = exec_worker(request.attempt_id, store=store, provider=FakeProvider([]), runner=runner)
    assert result.outcome is AttemptOutcome.DEFERRED


def test_integration_error_receipt(store: StateStore) -> None:
    request = _bound_request()
    store.write_request(request)
    runner = FakeRunner(
        [
            child_result(exit_code=0, stdout_tail="0.1.0"),
            child_result(exit_code=2, stdout_tail=None),
        ]
    )
    result = exec_worker(request.attempt_id, store=store, provider=FakeProvider([]), runner=runner)
    assert result.outcome is AttemptOutcome.INTEGRATION_ERROR


def test_blocked_when_still_running(store: StateStore) -> None:
    request = _bound_request()
    store.write_request(request)
    runner = FakeRunner([])
    result = exec_worker(
        request.attempt_id, store=store, provider=_running_provider(), runner=runner
    )
    assert result.outcome is AttemptOutcome.BLOCKED
    assert runner.calls == []
    receipt = store.read_receipt(request.attempt_id)
    assert receipt is not None and receipt.blocked_reason


@pytest.mark.parametrize(
    "matcher",
    [
        {},
        {"entrypoints": []},
        {"entrypoints": [{}]},
        {"entrypoints": None},
        {"entrypoints": [{"kind": "unknown", "value": "/opt/app/run_app"}]},
        {"entrypoints": [{"kind": "script", "value": 42}]},
        {"entrypoints": [{"kind": "script", "value": "relative"}]},
        {**MATCHER, "argv_tokens": "--dir"},
        {**MATCHER, "argv_tokens": [42]},
        {**MATCHER, "cmdline_regex": "["},
        {**MATCHER, "env_eq": {"KEY": 42}},
        {**MATCHER, "exe_realpath": False},
    ],
)
def test_invalid_persisted_matcher_never_authorizes_backup(
    store: StateStore, matcher: dict
) -> None:
    request = replace(_bound_request(), matcher=matcher)
    store.write_request(request)
    runner = FakeRunner([])

    with pytest.raises(StateError, match=r"request\.matcher is malformed"):
        exec_worker(request.attempt_id, store=store, provider=_running_provider(), runner=runner)

    assert runner.calls == []
    assert store.read_receipt(request.attempt_id) is None


def test_refuses_replay(store: StateStore) -> None:
    request = _bound_request()
    store.write_request(request)
    store.write_receipt(
        Receipt(
            attempt_id=request.attempt_id,
            request_id=request.request_id,
            instance_id=request.instance_id,
            outcome=AttemptOutcome.CREATED,
            started_at=utc_now(),
            ended_at=utc_now(),
            duration_seconds=1.0,
        )
    )
    runner = FakeRunner([])
    result = exec_worker(request.attempt_id, store=store, provider=FakeProvider([]), runner=runner)
    assert result.outcome is AttemptOutcome.CREATED
    assert runner.calls == []


def test_missing_request_is_integration_error(store: StateStore) -> None:
    with pytest.raises(IntegrationError):
        exec_worker(new_uuid(), store=store, provider=FakeProvider([]), runner=FakeRunner([]))


def test_mismatched_attempt_id_is_integration_error(store: StateStore) -> None:
    request = _bound_request()
    store.write_request(request)
    with pytest.raises(IntegrationError):
        exec_worker(
            "other-attempt",
            store=store,
            provider=FakeProvider([]),
            runner=FakeRunner([]),
        )


def test_default_runner_executes_fake_binary_and_writes_receipt(
    store: StateStore, tmp_path: Path
) -> None:
    executable = tmp_path / "fake-vaultkeep"
    executable.write_text(
        '#!/bin/sh\nif [ "$1" = --version ]; then printf "0.1.0\\n"; '
        'else printf "run: created\\n"; fi\n'
    )
    executable.chmod(0o700)
    request = replace(_bound_request(), vaultkeep=str(executable), preflight_version="0.1.0")
    store.write_request(request)
    result = exec_worker(request.attempt_id, store=store, provider=FakeProvider([]))
    assert result.outcome is AttemptOutcome.CREATED
    receipt = store.read_receipt(request.attempt_id)
    assert receipt is not None and receipt.marker_line == "run: created"


def test_execution_version_mismatch_warns(store: StateStore, caplog) -> None:
    request = replace(_bound_request(), preflight_version="0.1.0")
    store.write_request(request)
    runner = FakeRunner(
        [child_result(stdout_tail="0.2.0"), child_result(stdout_tail="run: created")]
    )
    exec_worker(request.attempt_id, store=store, provider=FakeProvider([]), runner=runner)
    assert "differs from preflight" in caplog.text
