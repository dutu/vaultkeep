"""Workflow-level lifecycle-hook ordering and state reporting tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vaultkeep import workflow
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
    monkeypatch.setattr(workflow, "load_validated_config", lambda path: config)
    monkeypatch.setattr(workflow, "_validate_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(workflow, "_validate_configured_hooks", lambda value: None)
    monkeypatch.setattr(workflow, "job_identity_hash", lambda *args: "a" * 16)
    monkeypatch.setattr(workflow, "job_state_path", lambda *args, **kwargs: tmp_path / "state.json")
    monkeypatch.setattr(workflow, "discover_sources", lambda value: object())
    monkeypatch.setattr(workflow, "calculate_source_digest", lambda value: "sha256:" + "a" * 64)
    monkeypatch.setattr(
        workflow, "calculate_config_fingerprint", lambda value: "sha256:" + "b" * 64
    )
    monkeypatch.setattr(workflow, "discover_backups", lambda value: next(discovered))
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
    monkeypatch.setattr(workflow, "build_archive", lambda *args, **kwargs: object())
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
    ) -> None:
        del config, path, context
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
        tmp_path / "app.yaml", paths=workflow.WorkflowPaths(tmp_path, tmp_path)
    )

    assert result.result == "created"
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
        phase: str, config: JobConfig, path: Path, context: Any, outcomes: list[HookOutcomeState]
    ) -> None:
        del config, path, context
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
        workflow.run_backup(tmp_path / "app.yaml", paths=workflow.WorkflowPaths(tmp_path, tmp_path))

    assert phases == ["before_check", "before_archive", "after_archive", "on_failure"]
    assert [outcome.phase for outcome in states[-1].last_run.hooks] == phases
