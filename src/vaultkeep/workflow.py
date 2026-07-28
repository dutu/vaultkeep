"""Manual backup workflow and command-oriented validation."""

from __future__ import annotations

import os
import socket
import tempfile
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from vaultkeep.archive import ArchiveBuildRequest, build_archive, load_password_file
from vaultkeep.config import JobConfig, load_config, resolve_runtime_config
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


def user_workflow_paths() -> WorkflowPaths:
    """Return per-user state, temp, and lock paths for non-root executions."""
    state_base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    cache_base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    runtime_base = os.environ.get("XDG_RUNTIME_DIR")
    lock_base = Path(runtime_base) if runtime_base else cache_base
    return WorkflowPaths(
        state_root=state_base / "vaultkeep" / "jobs",
        local_temp_root=cache_base / "vaultkeep" / "tmp",
        lock_root=lock_base / "vaultkeep" / "locks",
    )


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Stable facts returned by a manual command without presentation concerns."""

    command: str
    result: str
    backups: int = 0
    removed: int = 0
    archive_path: Path | None = None


class _HookRunKwargs(TypedDict, total=False):
    owner_uid: int
    owner_gid: int | None
    allow_root_owned: bool


def load_validated_config(
    config_path: Path, *, runtime_params: Mapping[str, str] | None = None
) -> JobConfig:
    """Load one configuration and run all non-environment validation."""
    parameters = dict(runtime_params or {})
    config = resolve_runtime_config(load_config(config_path), parameters)
    validate_semantics(config, config_path=config_path, runtime_params=parameters)
    return config


def validate_job(
    config_path: Path,
    *,
    schema_only: bool = False,
    runtime_params: Mapping[str, str] | None = None,
) -> CommandResult:
    """Validate configuration, optionally including the runtime destination/source checks."""
    if schema_only:
        load_config(config_path)
        return CommandResult("validate", "valid")

    config = load_validated_config(config_path, runtime_params=runtime_params)
    _validate_runtime(config, require_sources=True, require_writable_destination=False)
    return CommandResult("validate", "valid")


def list_backups(
    config_path: Path, *, runtime_params: Mapping[str, str] | None = None
) -> tuple[CommandResult, tuple[object, ...]]:
    """Discover and report valid backups without requiring configured sources."""
    parameters = dict(runtime_params or {})
    config = load_validated_config(config_path, runtime_params=parameters)
    _validate_runtime(config, require_sources=False, require_writable_destination=False)
    discovered = discover_backups(config, runtime_params=parameters)
    return CommandResult("list", "listed", backups=len(discovered.backups)), discovered.backups


def prune_backups(
    config_path: Path, *, dry_run: bool, runtime_params: Mapping[str, str] | None = None
) -> CommandResult:
    """Calculate or execute retention without touching configured sources."""
    parameters = dict(runtime_params or {})
    config = load_validated_config(config_path, runtime_params=parameters)
    _validate_runtime(config, require_sources=False, require_writable_destination=not dry_run)
    discovered = discover_backups(config, runtime_params=parameters)
    plan = build_prune_plan(discovered, config.retention)
    removed = () if dry_run else execute_prune_plan(plan, discovered)
    return CommandResult("prune", "planned" if dry_run else "pruned", removed=len(removed))


def verify_backups(
    config_path: Path, *, runtime_params: Mapping[str, str] | None = None
) -> CommandResult:
    """Discovery already verifies sidecars; command exposes its structural result."""
    parameters = dict(runtime_params or {})
    config = load_validated_config(config_path, runtime_params=parameters)
    _validate_runtime(config, require_sources=False, require_writable_destination=False)
    discovered = discover_backups(config, runtime_params=parameters)
    if discovered.malformed:
        raise DestinationError(
            "Matching malformed destination entries prevent successful verification"
        )
    return CommandResult("verify", "verified", backups=len(discovered.backups))


def run_backup(
    config_path: Path,
    *,
    runtime_params: Mapping[str, str] | None = None,
    paths: WorkflowPaths | None = None,
    user_mode: bool = False,
) -> CommandResult:
    """Execute change detection, archival, commit, state persistence, and retention."""
    if paths is None:
        paths = user_workflow_paths() if user_mode else WorkflowPaths()
    owner_uid, owner_gid = _runtime_owner(user_mode)
    if user_mode:
        _prepare_user_paths(paths)
    hook_kwargs: _HookRunKwargs = (
        {"owner_uid": owner_uid, "owner_gid": owner_gid, "allow_root_owned": True}
        if user_mode
        else {}
    )
    parameters = dict(runtime_params or {})
    config = load_validated_config(config_path, runtime_params=parameters)
    _validate_runtime(config, require_sources=True, require_writable_destination=True)
    if user_mode:
        _validate_configured_hooks(config, user_mode=True)
    else:
        _validate_configured_hooks(config)
    identity = job_identity_hash(config_path, config.job.id, runtime_params=parameters)
    lock = JobLock(
        job_lock_path(root=paths.lock_root, job_id=config.job.id, identity_hash=identity)
    )
    lock.acquire()
    state_path = job_state_path(
        config_path, config.job.id, runtime_params=parameters, state_root=paths.state_root
    )
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
            **hook_kwargs,
        )
        snapshot = discover_sources(config)
        source_digest = calculate_source_digest(snapshot)
        config_fingerprint = calculate_config_fingerprint(config, runtime_params=parameters)
        discovered = discover_backups(config, runtime_params=parameters)
        loaded_password = (
            load_password_file(
                Path(config.encryption.password_file),
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
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
                **hook_kwargs,
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
            runtime_params=parameters,
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
            _run_configured_hook(
                "before_archive",
                config,
                config_path,
                hook_context,
                hook_outcomes,
                **hook_kwargs,
            )
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
                    private_owner_uid=owner_uid,
                    private_owner_gid=owner_gid,
                ),
                password=loaded_password.secret if loaded_password else None,
            )
        except BaseException:
            try:
                after_archive_attempted = True
                _run_configured_hook(
                    "after_archive",
                    config,
                    config_path,
                    hook_context,
                    hook_outcomes,
                    **hook_kwargs,
                )
            except BaseException:
                pass
            raise
        if not after_archive_attempted:
            _run_configured_hook(
                "after_archive",
                config,
                config_path,
                hook_context,
                hook_outcomes,
                **hook_kwargs,
            )
        manifest = commit_archive_artifact(
            allocated,
            artifact,
            application_version=installed_version(),
            job_id=config.job.id,
            hostname=socket.gethostname(),
            created_at=now,
            config_fingerprint=config_fingerprint,
        )
        committed = discover_backups(config, runtime_params=parameters)
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
            **hook_kwargs,
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
                    "on_failure",
                    config,
                    config_path,
                    failure_context,
                    hook_outcomes,
                    **hook_kwargs,
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
    if config.destination.mount_point is not None:
        _assert_configured_mount_point(root, Path(config.destination.mount_point))
    if require_writable_destination:
        _assert_writable_destination(root)
    if (
        config.destination.marker_file is not None
        and not (root / config.destination.marker_file).is_file()
    ):
        raise DestinationError("Configured destination marker is missing")
    if require_sources:
        for source in config.sources:
            if not Path(source.path).exists() and not config.source_options.ignore_missing:
                raise DestinationError(f"Configured source does not exist: {source.path}")


def _assert_configured_mount_point(root: Path, mount_point: Path) -> None:
    if not mount_point.is_dir():
        raise DestinationError(
            f"Configured mount point is not an accessible directory: {mount_point}"
        )
    try:
        root_resolved = root.resolve(strict=False)
        mount_point_resolved = mount_point.resolve(strict=False)
    except OSError as error:
        raise DestinationError(
            f"Unable to resolve configured destination mount point: {mount_point}"
        ) from error
    if not _path_is_relative_to(root_resolved, mount_point_resolved):
        raise DestinationError(f"Destination root is not below configured mount point: {root}")
    if not os.path.ismount(mount_point):
        raise DestinationError(f"Configured mount point is not mounted: {mount_point}")


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _assert_writable_destination(root: Path) -> None:
    try:
        with tempfile.TemporaryFile(prefix=".vaultkeep-write-test-", dir=root):
            pass
    except OSError as error:
        raise DestinationError(f"Destination root is not writable: {root}") from error


def _runtime_owner(user_mode: bool) -> tuple[int, int | None]:
    if user_mode:
        geteuid = getattr(os, "geteuid", None)
        if callable(geteuid):
            return int(geteuid()), None
    return 0, 0


def _prepare_user_paths(paths: WorkflowPaths) -> None:
    for path in (paths.state_root, paths.local_temp_root, paths.lock_root):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(path, 0o700)


def _validate_configured_hooks(config: JobConfig, *, user_mode: bool = False) -> None:
    owner_uid, owner_gid = _runtime_owner(user_mode)
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
            validate_hook_executable(
                hook.command,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
                allow_root_owned=user_mode,
            )


def _run_configured_hook(
    phase: HookPhase,
    config: JobConfig,
    config_path: Path,
    context: HookContext,
    outcomes: list[HookOutcomeState],
    owner_uid: int = 0,
    owner_gid: int | None = 0,
    allow_root_owned: bool = False,
) -> None:
    del config_path
    hook = getattr(config.hooks, phase)
    if hook is not None:
        execution = run_hook(
            phase,
            hook,
            context,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            allow_root_owned=allow_root_owned,
        )
        outcomes.append(execution.outcome)
        require_success(execution)
