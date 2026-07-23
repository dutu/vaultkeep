"""MS6 manual backup workflow and command-oriented validation."""

from __future__ import annotations

import socket
import uuid
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from vaultkeep.archive import ArchiveBuildRequest, build_archive, load_password_file
from vaultkeep.config import JobConfig, load_config
from vaultkeep.destination import (
    allocate_job_backup_paths,
    build_prune_plan,
    commit_archive_artifact,
    create_staging_directory,
    discover_backups,
    execute_prune_plan,
)
from vaultkeep.errors import DestinationError
from vaultkeep.hooks import HookContext, require_success, run_hook, validate_hook_executable
from vaultkeep.locking import JobLock, job_lock_path
from vaultkeep.sources import (
    calculate_config_fingerprint,
    calculate_source_digest,
    discover_sources,
)
from vaultkeep.state.identity import job_identity_hash, job_state_path
from vaultkeep.state.local_state import reconcile_local_state
from vaultkeep.state.models import HookOutcomeState, HookPhase, LocalState
from vaultkeep.state.transitions import (
    state_after_created,
    state_after_failed,
    state_after_unchanged,
)
from vaultkeep.state.unchanged import evaluate_unchanged
from vaultkeep.validation import validate_semantics
from vaultkeep.version import installed_version


@dataclass(frozen=True, slots=True)
class WorkflowPaths:
    """Explicit testable locations for local state and private archive workspaces."""

    state_root: Path = Path("/var/lib/vaultkeep/jobs")
    local_temp_root: Path = Path("/var/lib/vaultkeep/tmp")
    lock_root: Path = Path("/run/lock/vaultkeep")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Stable facts returned by a manual command without presentation concerns."""

    command: str
    result: str
    backups: int = 0
    removed: int = 0
    archive_path: Path | None = None


def load_validated_config(config_path: Path) -> JobConfig:
    """Load one configuration and run all non-environment validation."""
    config = load_config(config_path)
    validate_semantics(config, config_path=config_path)
    return config


def validate_job(config_path: Path, *, schema_only: bool = False) -> CommandResult:
    """Validate configuration, optionally including the runtime destination/source checks."""
    config = load_validated_config(config_path)
    if not schema_only:
        _validate_runtime(config, require_sources=True, require_writable_destination=False)
    return CommandResult("validate", "valid")


def list_backups(config_path: Path) -> tuple[CommandResult, tuple[object, ...]]:
    """Discover and report valid backups without requiring configured sources."""
    config = load_validated_config(config_path)
    _validate_runtime(config, require_sources=False, require_writable_destination=False)
    discovered = discover_backups(config)
    return CommandResult("list", "listed", backups=len(discovered.backups)), discovered.backups


def prune_backups(config_path: Path, *, dry_run: bool) -> CommandResult:
    """Calculate or execute retention without touching configured sources."""
    config = load_validated_config(config_path)
    _validate_runtime(config, require_sources=False, require_writable_destination=not dry_run)
    discovered = discover_backups(config)
    plan = build_prune_plan(discovered, config.retention)
    removed = () if dry_run else execute_prune_plan(plan, discovered)
    return CommandResult("prune", "planned" if dry_run else "pruned", removed=len(removed))


def verify_backups(config_path: Path) -> CommandResult:
    """Discovery already verifies sidecars; command exposes its structural result in MS6."""
    config = load_validated_config(config_path)
    _validate_runtime(config, require_sources=False, require_writable_destination=False)
    discovered = discover_backups(config)
    if discovered.malformed:
        raise DestinationError(
            "Matching malformed destination entries prevent successful verification"
        )
    return CommandResult("verify", "verified", backups=len(discovered.backups))


def run_backup(config_path: Path, *, paths: WorkflowPaths | None = None) -> CommandResult:
    """Execute change detection, archival, commit, state persistence, and retention."""
    if paths is None:
        paths = WorkflowPaths()
    config = load_validated_config(config_path)
    _validate_runtime(config, require_sources=True, require_writable_destination=True)
    _validate_configured_hooks(config)
    identity = job_identity_hash(config_path, config.job.id)
    lock = JobLock(
        job_lock_path(root=paths.lock_root, job_id=config.job.id, identity_hash=identity)
    )
    lock.acquire()
    state_path = job_state_path(config_path, config.job.id, state_root=paths.state_root)
    hook_outcomes: list[HookOutcomeState] = []
    reconciliation = None
    latest_state: LocalState | None = None
    loaded_password = None
    failure_context = HookContext(config.job.id, config_path, destination=config.destination.root)
    terminal_hook_failed = False

    try:
        _run_configured_hook(
            "before_check",
            config,
            config_path,
            HookContext(config.job.id, config_path),
            hook_outcomes,
        )
        snapshot = discover_sources(config)
        source_digest = calculate_source_digest(snapshot)
        config_fingerprint = calculate_config_fingerprint(config)
        discovered = discover_backups(config)
        loaded_password = (
            load_password_file(Path(config.encryption.password_file))
            if config.encryption.password_file
            else None
        )
        reconciliation = reconcile_local_state(
            state_path,
            job_id=config.job.id,
            identity_hash=identity,
            application_version=installed_version(),
            destination_backups=discovered.state_records,
            current_credential=loaded_password.fingerprint if loaded_password else None,
        )
        latest_state = reconciliation.state
        decision = evaluate_unchanged(
            reconciliation.state,
            source_digest=source_digest,
            config_fingerprint=config_fingerprint,
            current_credential=loaded_password.fingerprint if loaded_password else None,
            destination_backups=discovered.state_records,
        )
        now = datetime.now().astimezone()
        if decision.unchanged:
            terminal_hook_failed = True
            _run_configured_hook(
                "on_unchanged",
                config,
                config_path,
                HookContext(
                    config.job.id,
                    config_path,
                    source_digest=source_digest,
                    destination=config.destination.root,
                    result="unchanged",
                    version=installed_version(),
                ),
                hook_outcomes,
            )
            terminal_hook_failed = False
            from vaultkeep.state.atomic import write_local_state

            latest_state = state_after_unchanged(
                reconciliation.state,
                run_at=now,
                application_version=installed_version(),
                credential_fingerprint=loaded_password.fingerprint if loaded_password else None,
                hooks=tuple(hook_outcomes),
            )
            write_local_state(
                state_path,
                latest_state,
            )
            previous = reconciliation.state.last_successful_backup
            if previous is None:
                raise AssertionError("Unchanged state has no successful backup")
            return CommandResult("run", "unchanged", archive_path=Path(previous.backup_path))
        backup_id = uuid.uuid4().hex
        allocated = allocate_job_backup_paths(
            config.destination,
            job_id=config.job.id,
            backup_id=backup_id,
            hostname=socket.gethostname(),
            created_at=now,
            source_digest=source_digest,
            archive_format=config.archive.format,
        )
        create_staging_directory(allocated)
        hook_context = HookContext(
            config.job.id,
            config_path,
            backup_id=backup_id,
            source_digest=source_digest,
            destination=config.destination.root,
            archive=str(allocated.final_directory / allocated.archive_path.name),
            backup_directory=str(allocated.final_directory),
            version=installed_version(),
        )
        failure_context = hook_context
        after_archive_attempted = False
        try:
            _run_configured_hook("before_archive", config, config_path, hook_context, hook_outcomes)
            artifact = build_archive(
                ArchiveBuildRequest(
                    snapshot=snapshot,
                    expected_source_digest=source_digest,
                    archive_format=config.archive.format,
                    compression_level=config.archive.compression_level,
                    archive_path=allocated.archive_path,
                    checksum_path=allocated.checksum_path,
                    job_id=config.job.id,
                    job_identity_hash=identity,
                    backup_id=backup_id,
                    local_temp_root=paths.local_temp_root,
                ),
                password=loaded_password.secret if loaded_password else None,
            )
        except BaseException:
            try:
                after_archive_attempted = True
                _run_configured_hook(
                    "after_archive", config, config_path, hook_context, hook_outcomes
                )
            except BaseException:
                pass
            raise
        if not after_archive_attempted:
            _run_configured_hook("after_archive", config, config_path, hook_context, hook_outcomes)
        manifest = commit_archive_artifact(
            allocated,
            artifact,
            application_version=installed_version(),
            job_id=config.job.id,
            hostname=socket.gethostname(),
            created_at=now,
            config_fingerprint=config_fingerprint,
        )
        committed = discover_backups(config)
        record = next(
            record for record in committed.state_records if record.backup_id == manifest.backup_id
        )
        from vaultkeep.state.atomic import write_local_state

        latest_state = state_after_created(
            job_id=config.job.id,
            identity_hash=identity,
            backup=record,
            run_at=now,
            application_version=installed_version(),
            credential_fingerprint=loaded_password.fingerprint if loaded_password else None,
            hooks=tuple(hook_outcomes),
        )
        write_local_state(
            state_path,
            latest_state,
        )
        plan = build_prune_plan(committed, config.retention)
        removed = execute_prune_plan(plan, committed)
        terminal_hook_failed = True
        _run_configured_hook(
            "on_success",
            config,
            config_path,
            replace(hook_context, result="created"),
            hook_outcomes,
        )
        terminal_hook_failed = False
        latest_state = state_after_created(
            job_id=config.job.id,
            identity_hash=identity,
            backup=record,
            run_at=now,
            application_version=installed_version(),
            credential_fingerprint=loaded_password.fingerprint if loaded_password else None,
            hooks=tuple(hook_outcomes),
        )
        write_local_state(state_path, latest_state)
        return CommandResult(
            "run", "created", removed=len(removed), archive_path=Path(record.backup_path)
        )
    except BaseException as error:
        failure_context = replace(
            failure_context,
            result="failed",
            failed_stage="run",
            error=str(error)[:512],
            version=installed_version(),
        )
        if not terminal_hook_failed:
            with suppress(BaseException):
                _run_configured_hook(
                    "on_failure", config, config_path, failure_context, hook_outcomes
                )
        if latest_state is not None:
            from vaultkeep.state.atomic import write_local_state

            with suppress(BaseException):
                write_local_state(
                    state_path,
                    state_after_failed(
                        latest_state,
                        run_at=datetime.now().astimezone(),
                        application_version=installed_version(),
                        hooks=tuple(hook_outcomes),
                    ),
                )
        raise
    finally:
        if loaded_password is not None:
            loaded_password.secret.clear()
        lock.release()


def _validate_runtime(
    config: JobConfig, *, require_sources: bool, require_writable_destination: bool
) -> None:
    root = Path(config.destination.root)
    if not root.is_dir():
        raise DestinationError(f"Destination root is not an accessible directory: {root}")
    if require_writable_destination and not root.exists():
        raise DestinationError(f"Destination root is not writable: {root}")
    if (
        config.destination.marker_file is not None
        and not (root / config.destination.marker_file).is_file()
    ):
        raise DestinationError("Configured destination marker is missing")
    if require_sources:
        for source in config.sources:
            if not Path(source.path).exists() and not config.source_options.ignore_missing:
                raise DestinationError(f"Configured source does not exist: {source.path}")


def _validate_configured_hooks(config: JobConfig) -> None:
    for phase in (
        "before_check",
        "before_archive",
        "after_archive",
        "on_success",
        "on_failure",
        "on_unchanged",
    ):
        hook = getattr(config.hooks, phase)
        if hook is not None:
            validate_hook_executable(hook.command)


def _run_configured_hook(
    phase: HookPhase,
    config: JobConfig,
    config_path: Path,
    context: HookContext,
    outcomes: list[HookOutcomeState],
) -> None:
    del config_path
    hook = getattr(config.hooks, phase)
    if hook is not None:
        execution = run_hook(phase, hook, context)
        outcomes.append(execution.outcome)
        require_success(execution)
