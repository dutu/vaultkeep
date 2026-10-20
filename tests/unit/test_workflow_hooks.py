"""Workflow-level lifecycle-hook ordering and state reporting tests."""

from __future__ import annotations

import shlex
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vaultkeep import hooks, workflow
from vaultkeep.config.models import JobConfig
from vaultkeep.errors import DestinationError, HookError
from vaultkeep.state.models import BackupStateRecord, HookOutcomeState, LocalState


def _config(valid_config: dict[str, Any]) -> JobConfig:
    return JobConfig.model_validate(valid_config)


def _record() -> BackupStateRecord:
    return BackupStateRecord(
        job_id="app",
        backup_id="a" * 32,
        created_at_utc="2026-01-01T00:00:00Z",
        source_digest="sha256:" + "a" * 64,
        config_fingerprint="sha256:" + "b" * 64,
        backup_path="/mnt/backups/app/backup.tar.zst",
        application_version="1.0.0",
        encrypted=False,
    )


def _empty_state() -> LocalState:
    return LocalState(
        job_id="app",
        job_identity_hash="a" * 16,
        application_version="1.0.0",
    )


@pytest.mark.parametrize("user_mode", [False, True])
def test_hook_preflight_validates_shell_for_execution_identity(
    monkeypatch: pytest.MonkeyPatch, valid_config: dict[str, Any], user_mode: bool
) -> None:
    valid_config["hooks"]["on_success"] = {"command": "printf success"}
    calls: list[dict[str, object]] = []

    def validate(**kwargs: object) -> Path:
        calls.append(kwargs)
        return Path("/bin/sh")

    monkeypatch.setattr(workflow, "validate_hook_shell", validate)

    workflow._validate_configured_hooks(_config(valid_config), user_mode=user_mode)

    owner_uid, owner_gid = workflow._runtime_owner(user_mode)
    assert calls == [
        {"owner_uid": owner_uid, "owner_gid": owner_gid, "allow_root_owned": user_mode}
    ]


def _patch_creation_workflow(
    monkeypatch: pytest.MonkeyPatch, config: JobConfig, tmp_path: Path
) -> list[LocalState]:
    """Replace archive I/O with stable values while leaving lifecycle control intact."""
    record = _record()
    discovered = iter(
        [
            SimpleNamespace(state_records=(), backups=()),
            SimpleNamespace(state_records=(record,), backups=()),
        ]
    )
    states: list[LocalState] = []
    allocated = SimpleNamespace(
        final_directory=tmp_path / "final",
        archive_path=tmp_path / "stage" / "archive.tar.zst",
        checksum_path=tmp_path / "stage" / "archive.tar.zst.sha256",
    )
    monkeypatch.setattr(workflow, "load_validated_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(workflow, "_validate_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(workflow, "_validate_configured_hooks", lambda value: None)
    monkeypatch.setattr(workflow, "job_identity_hash", lambda *args, **kwargs: "a" * 16)
    monkeypatch.setattr(workflow, "job_state_path", lambda *args, **kwargs: tmp_path / "state.json")
    monkeypatch.setattr(workflow, "discover_sources", lambda value: object())
    monkeypatch.setattr(workflow, "calculate_source_digest", lambda value: "sha256:" + "a" * 64)
    monkeypatch.setattr(
        workflow, "calculate_config_fingerprint", lambda *args, **kwargs: "sha256:" + "b" * 64
    )
    monkeypatch.setattr(workflow, "discover_backups", lambda *args, **kwargs: next(discovered))
    monkeypatch.setattr(
        workflow,
        "reconcile_local_state",
        lambda *args, **kwargs: SimpleNamespace(state=_empty_state()),
    )
    monkeypatch.setattr(
        workflow,
        "evaluate_unchanged",
        lambda *args, **kwargs: SimpleNamespace(unchanged=False),
    )
    monkeypatch.setattr(workflow, "allocate_job_backup_paths", lambda *args, **kwargs: allocated)
    monkeypatch.setattr(workflow, "create_staging_directory", lambda value: None)
    monkeypatch.setattr(
        workflow, "build_archive", lambda *args, **kwargs: SimpleNamespace(size=1234)
    )
    monkeypatch.setattr(
        workflow,
        "commit_archive_artifact",
        lambda *args, **kwargs: SimpleNamespace(backup_id=record.backup_id),
    )
    monkeypatch.setattr(workflow, "build_prune_plan", lambda *args: object())
    monkeypatch.setattr(workflow, "execute_prune_plan", lambda *args: ())
    monkeypatch.setattr(workflow, "installed_version", lambda: "1.0.0")
    monkeypatch.setattr(
        "vaultkeep.state.atomic.write_local_state", lambda path, state: states.append(state)
    )
    return states


@pytest.mark.parametrize("fail_before_check", [False, True])
def test_workflow_executes_terminal_shell_hook_with_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    valid_config: dict[str, Any],
    fail_before_check: bool,
) -> None:
    output = tmp_path / "notification.txt"
    command = (
        'printf "%s\\n" "{job}" "{format}" "{source_hash:.8}" '
        '"$VAULTKEEP_RESULT" "$VAULTKEEP_ERROR" '
        f"> {shlex.quote(str(output))}"
    )
    valid_config["hooks"]["on_success"] = {"command": command}
    valid_config["hooks"]["on_failure"] = {"command": command}
    if fail_before_check:
        valid_config["hooks"]["before_check"] = {"command": "exit 4"}
    _patch_creation_workflow(monkeypatch, _config(valid_config), tmp_path)
    monkeypatch.setattr(hooks, "validate_hook_shell", lambda: Path("/bin/sh"))
    paths = workflow.WorkflowPaths(tmp_path, tmp_path, tmp_path / "locks")

    if fail_before_check:
        with pytest.raises(HookError, match="before_check hook exited with status 4"):
            workflow.run_backup(tmp_path / "app.yaml", paths=paths)
        assert output.read_text(encoding="utf-8").splitlines() == [
            "app",
            "tar.zst",
            "",
            "failed",
            "before_check hook exited with status 4",
        ]
    else:
        result = workflow.run_backup(tmp_path / "app.yaml", paths=paths)
        assert result.result == "created"
        assert output.read_text(encoding="utf-8").splitlines() == [
            "app",
            "tar.zst",
            "aaaaaaaa",
            "created",
            "",
        ]


def test_failure_hook_has_source_hash_when_destination_discovery_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    output = tmp_path / "failure.txt"
    valid_config["hooks"]["on_failure"] = {
        "command": f'printf "%s" "{{source_hash:.12}}" > {shlex.quote(str(output))}'
    }
    _patch_creation_workflow(monkeypatch, _config(valid_config), tmp_path)
    monkeypatch.setattr(hooks, "validate_hook_shell", lambda: Path("/bin/sh"))

    def discover(*args: Any, **kwargs: Any) -> None:
        raise DestinationError("destination discovery failed")

    monkeypatch.setattr(workflow, "discover_backups", discover)

    with pytest.raises(DestinationError, match="destination discovery failed"):
        workflow.run_backup(
            tmp_path / "app.yaml",
            paths=workflow.WorkflowPaths(tmp_path, tmp_path, tmp_path / "locks"),
        )

    assert output.read_text(encoding="utf-8") == "a" * 12


@pytest.mark.parametrize("unchanged", [False, True])
def test_terminal_template_has_current_run_time_and_builtin_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    valid_config: dict[str, Any],
    unchanged: bool,
) -> None:
    output = tmp_path / "notification.txt"
    command = (
        'printf "%s\\n" "{job}" "{hostname}" "{source_hash:.12}" "{format}" "{timestamp_utc}" '
        f"> {shlex.quote(str(output))}"
    )
    valid_config["hooks"]["on_success"] = {"command": command}
    valid_config["hooks"]["on_unchanged"] = {"command": command}
    states = _patch_creation_workflow(monkeypatch, _config(valid_config), tmp_path)
    monkeypatch.setattr(hooks, "validate_hook_shell", lambda: Path("/bin/sh"))
    monkeypatch.setattr(workflow.socket, "gethostname", lambda: "backup-host")
    previous = workflow.state_after_created(
        job_id="app",
        identity_hash="a" * 16,
        backup=_record(),
        run_at=datetime.fromisoformat(_record().created_at_utc),
        application_version="1.0.0",
    )
    monkeypatch.setattr(
        workflow, "reconcile_local_state", lambda *args, **kwargs: SimpleNamespace(state=previous)
    )
    monkeypatch.setattr(
        workflow, "evaluate_unchanged", lambda *args, **kwargs: SimpleNamespace(unchanged=unchanged)
    )

    result = workflow.run_backup(
        tmp_path / "app.yaml", paths=workflow.WorkflowPaths(tmp_path, tmp_path, tmp_path / "locks")
    )

    assert result.result == ("unchanged" if unchanged else "created")
    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines[:4] == ["app", "backup-host", "a" * 12, "tar.zst"]
    assert datetime.fromisoformat(lines[4]).replace(microsecond=0) == datetime.fromisoformat(
        states[-1].last_run.at_utc
    )


def test_created_run_orders_hooks_and_persists_outcomes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    states = _patch_creation_workflow(monkeypatch, _config(valid_config), tmp_path)
    phases: list[str] = []

    def run_phase(
        phase: str,
        config: JobConfig,
        path: Path,
        context: Any,
        outcomes: list[HookOutcomeState],
        **kwargs: Any,
    ) -> None:
        del config, path, context, kwargs
        phases.append(phase)
        outcomes.append(
            HookOutcomeState(
                phase=phase,
                duration_seconds=0,
                exit_code=0,
                timed_out=False,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        )

    monkeypatch.setattr(workflow, "_run_configured_hook", run_phase)

    result = workflow.run_backup(
        tmp_path / "app.yaml", paths=workflow.WorkflowPaths(tmp_path, tmp_path, tmp_path / "locks")
    )

    assert result.result == "created"
    assert result.archive_size == 1234
    assert result.archive_path == Path(_record().backup_path)
    assert phases == ["before_check", "before_archive", "after_archive", "on_success"]
    assert [outcome.phase for outcome in states[-1].last_run.hooks] == phases


def test_archive_failure_keeps_primary_error_and_runs_cleanup_then_failure_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    states = _patch_creation_workflow(monkeypatch, _config(valid_config), tmp_path)
    phases: list[str] = []
    monkeypatch.setattr(
        workflow,
        "build_archive",
        lambda *args, **kwargs: (_ for _ in ()).throw(DestinationError("primary")),
    )

    def run_phase(
        phase: str,
        config: JobConfig,
        path: Path,
        context: Any,
        outcomes: list[HookOutcomeState],
        **kwargs: Any,
    ) -> None:
        del config, path, context, kwargs
        phases.append(phase)
        outcomes.append(
            HookOutcomeState(
                phase=phase,
                duration_seconds=0,
                exit_code=1 if phase == "after_archive" else 0,
                timed_out=False,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        )
        if phase == "after_archive":
            raise HookError("cleanup failed")

    monkeypatch.setattr(workflow, "_run_configured_hook", run_phase)

    with pytest.raises(DestinationError, match="primary"):
        workflow.run_backup(
            tmp_path / "app.yaml",
            paths=workflow.WorkflowPaths(tmp_path, tmp_path, tmp_path / "locks"),
        )

    assert phases == ["before_check", "before_archive", "after_archive", "on_failure"]
    assert [outcome.phase for outcome in states[-1].last_run.hooks] == phases


def test_before_archive_failure_runs_cleanup_then_failure_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    states = _patch_creation_workflow(monkeypatch, _config(valid_config), tmp_path)
    phases: list[str] = []

    def run_phase(
        phase: str,
        config: JobConfig,
        path: Path,
        context: Any,
        outcomes: list[HookOutcomeState],
        **kwargs: Any,
    ) -> None:
        del config, path, context, kwargs
        phases.append(phase)
        outcomes.append(
            HookOutcomeState(
                phase=phase,
                duration_seconds=0,
                exit_code=1 if phase == "before_archive" else 0,
                timed_out=False,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        )
        if phase == "before_archive":
            raise HookError("quiesce failed")

    monkeypatch.setattr(workflow, "_run_configured_hook", run_phase)

    with pytest.raises(HookError, match="quiesce failed"):
        workflow.run_backup(
            tmp_path / "app.yaml",
            paths=workflow.WorkflowPaths(tmp_path, tmp_path, tmp_path / "locks"),
        )

    assert phases == ["before_check", "before_archive", "after_archive", "on_failure"]
    assert [outcome.phase for outcome in states[-1].last_run.hooks] == phases
