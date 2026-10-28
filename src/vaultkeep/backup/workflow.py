"""Shared backup workflow and command-oriented validation."""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import stat
import subprocess
import uuid
from collections.abc import Callable, Mapping
from contextlib import nullcontext, suppress
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from vaultkeep.application import (
    ResolvedApplication,
    execution_policy,
    require_application_offline,
    resolve_application,
    runtime_owner,
    validate_application_directory,
)
from vaultkeep.archive import (
    ArchiveBuildRequest,
    ArchiveTools,
    build_archive,
    encrypted_backup_credential_verifier,
    load_password_file,
    verify_tar_7z,
    verify_tar_zstd,
)
from vaultkeep.archive.checksums import verify_checksum_sidecar
from vaultkeep.backup.execution import Execution
from vaultkeep.backup.paths import WorkflowPaths, user_workflow_paths
from vaultkeep.backup.results import failure_reason
from vaultkeep.config import JobConfig, RetentionConfig, load_config
from vaultkeep.destination import (
    allocate_job_backup_paths,
    build_prune_plan,
    commit_archive_artifact,
    create_staging_directory,
    discover_backups,
    execute_prune_plan,
    probe_destination_writability,
)
from vaultkeep.destination.discovery import (
    DiscoveredBackup,
    DiscoveryResult,
    MalformedDestinationEntry,
)
from vaultkeep.destination.identity import replacement_identity
from vaultkeep.destination.pruning import valid_replacement, verify_backup_checksum
from vaultkeep.destination.suspect import mark_backup_suspect, record_suspect_replacement
from vaultkeep.errors import (
    ApplicationIdentityError,
    ArchiveChecksumMismatchError,
    ArchiveCorruptionError,
    ArchiveVerificationError,
    DestinationError,
    PruneError,
    RetentionError,
    SourceDiscoveryError,
    VaultkeepError,
)
from vaultkeep.file_backups import (
    file_config,
    file_destinations,
    file_key,
    require_file_directory,
    select_files,
)
from vaultkeep.hooks import HookContext, require_success, run_hook, validate_hook_shell
from vaultkeep.locking import destination_lock
from vaultkeep.notifications.notifier import NotificationConfig
from vaultkeep.progress import VerificationProgress, report_progress
from vaultkeep.retention.planner import RetentionTier
from vaultkeep.sources import (
    SourceEntryType,
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
from vaultkeep.validation import prepare_config, prepare_job
from vaultkeep.version import installed_version


@dataclass(frozen=True, slots=True)
class FileBackupResult:
    """The outcome of one independent file within a per-file job."""

    source: Path
    result: str
    archive_path: Path | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Stable facts returned by a manual command without presentation concerns."""

    command: str
    result: str
    backups: int = 0
    removed: int = 0
    archive_path: Path | None = None
    archive_size: int | None = None
    files: tuple[FileBackupResult, ...] = ()
    cleanup_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BackupInventory:
    """Read-only inventory facts, including entries that could not be listed."""

    destination: Path
    backups: tuple[DiscoveredBackup, ...]
    malformed: tuple[MalformedDestinationEntry, ...]
    job: str
    retention: RetentionConfig
    retained_by: dict[Path, tuple[RetentionTier, ...] | None]
    retention_warnings: tuple[tuple[Path, str], ...]
    decisions: dict[Path, str]


class _HookRunKwargs(TypedDict, total=False):
    owner_uid: int
    owner_gid: int | None
    allow_root_owned: bool
    progress: Callable[[str], None]


def load_validated_config(
    config_path: Path,
    *,
    runtime_params: Mapping[str, str] | None = None,
    allow_disabled_retention: bool = False,
) -> JobConfig:
    """Prepare archive-inspection configuration without resolving application identity."""
    return prepare_config(
        load_config(config_path),
        config_path=config_path,
        runtime_params=runtime_params,
        allow_disabled_retention=allow_disabled_retention,
    )


def validate_job(
    config_path: Path,
    *,
    schema_only: bool = False,
    user_mode: bool = False,
    runtime_params: Mapping[str, str] | None = None,
) -> CommandResult:
    """Validate configuration, optionally including the runtime destination/source checks."""
    if schema_only:
        load_config(config_path)
        return CommandResult("validate", "valid")

    config, application = prepare_job(
        load_config(config_path),
        user_mode=user_mode,
        config_path=config_path,
        runtime_params=runtime_params,
    )
    validate_prepared_job(config, application, config_path=config_path)
    return CommandResult("validate", "valid")


def validate_prepared_job(
    config: JobConfig, application: ResolvedApplication | None, *, config_path: Path
) -> None:
    """Check readiness using a prepared identity without another account resolution."""
    _validate_runtime(config, require_sources=True, require_writable_destination=True)
    if application is not None:
        validate_application_directory(application, config_path=config_path)
    if config.backup_mode == "per_file":
        select_files(config)


def list_backups(
    config_path: Path,
    *,
    user_mode: bool = False,
    runtime_params: Mapping[str, str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> BackupInventory:
    """List backup metadata without reading archive contents or requiring sources."""
    runtime_owner(user_mode, config_path=config_path)
    report = progress if progress is not None else lambda message: None
    parameters = dict(runtime_params or {})
    report("Loading and validating configuration")
    config = load_validated_config(
        config_path, runtime_params=parameters, allow_disabled_retention=True
    )
    report("Checking destination")
    _validate_runtime(config, require_sources=False, require_writable_destination=False)
    report("Listing backup metadata")
    destinations = file_destinations(config) if config.backup_mode == "per_file" else (config,)
    histories: list[DiscoveryResult] = []
    for destination in destinations:
        try:
            histories.append(
                discover_backups(destination, runtime_params=parameters, metadata_only=True)
            )
        except (DestinationError, OSError) as error:
            histories.append(
                DiscoveryResult(
                    (), (MalformedDestinationEntry(Path(destination.destination.root), str(error)),)
                )
            )
    application = None
    if config.application is not None and config.consistency.mode == "application_offline":
        try:
            application = resolve_application(
                config.application, user_mode=user_mode, config_path=config_path
            )
        except ApplicationIdentityError as error:
            if error.issue.code == "user_unsupported":
                raise
            # Archive inventory does not require current-history authorization.

    discovered = tuple(histories)
    retained_by: dict[Path, tuple[RetentionTier, ...] | None] = {}
    decisions: dict[Path, str] = {}
    retention_warnings: list[tuple[Path, str]] = []
    for destination, history in zip(destinations, discovered, strict=True):
        decisions.update((backup.directory, "—") for backup in history.backups)
        try:
            history = _active_history(history, destination, application=application)
        except SourceDiscoveryError as error:
            decisions.update((backup.directory, "UNKNOWN") for backup in history.backups)
            retention_warnings.append((Path(destination.destination.root), str(error)))
            continue
        if not any(config.retention.model_dump().values()):
            for backup in history.backups:
                if backup.suspect_reason and valid_replacement(backup, history) is None:
                    decisions[backup.directory] = "PROTECTED"
            continue
        try:
            complete_plan = build_prune_plan(history, config.retention)
            plan = complete_plan.retention
        except (PruneError, RetentionError) as error:
            decisions.update((backup.directory, "UNKNOWN") for backup in history.backups)
            retained_by.update((backup.directory, None) for backup in history.backups)
            retention_warnings.append((Path(destination.destination.root), str(error)))
            continue
        tiers_by_directory: dict[Path, list[RetentionTier]] = {
            backup.directory: [] for backup in history.backups
        }
        for tier, selected in reversed(plan.selections.items()):
            for retained in selected:
                tiers_by_directory[retained.directory].append(tier)
        deleting = {backup.directory for backup in complete_plan.backups_to_delete}
        for backup in history.backups:
            decisions[backup.directory] = (
                "PRUNE"
                if backup.directory in deleting
                else "PROTECTED"
                if backup.suspect_reason
                else "KEEP"
            )
        retained_by.update(
            (directory, tuple(tiers)) for directory, tiers in tiers_by_directory.items()
        )
    backups = tuple(
        sorted(
            (backup for result in discovered for backup in result.backups),
            key=lambda backup: (backup.manifest.created_datetime_utc, str(backup.archive_path)),
            reverse=True,
        )
    )
    malformed = tuple(
        sorted(
            (entry for result in discovered for entry in result.malformed),
            key=lambda entry: str(entry.path),
        )
    )
    return BackupInventory(
        Path(config.destination.root),
        backups,
        malformed,
        config.job.id,
        config.retention,
        retained_by,
        tuple(retention_warnings),
        decisions,
    )


def prune_backups(
    config_path: Path,
    *,
    dry_run: bool,
    runtime_params: Mapping[str, str] | None = None,
    user_mode: bool = False,
) -> CommandResult:
    """Calculate or execute retention without touching configured sources."""
    parameters = dict(runtime_params or {})
    config, application = prepare_job(
        load_config(config_path),
        user_mode=user_mode,
        config_path=config_path,
        runtime_params=parameters,
    )
    _validate_runtime(config, require_sources=False, require_writable_destination=not dry_run)
    removed: list[Path] = []
    failures: list[str] = []
    with nullcontext() if dry_run else destination_lock(Path(config.destination.root)):
        destinations = file_destinations(config) if config.backup_mode == "per_file" else (config,)
        for destination in destinations:
            try:
                discovered = _active_history(
                    discover_backups(destination, runtime_params=parameters, metadata_only=True),
                    destination,
                    application=application,
                )
                plan = build_prune_plan(discovered, config.retention)
                if not dry_run:
                    removed.extend(execute_prune_plan(plan, discovered))
            except (VaultkeepError, OSError) as error:
                if isinstance(error, PruneError):
                    removed.extend(Path(path) for path in error.removed)
                failures.append(f"{destination.destination.root}: {error}")
    if failures:
        raise PruneError(
            f"Pruning stopped in affected histories after {len(removed)} deletions: "
            + "; ".join(failures),
            removed=tuple(str(path) for path in removed),
        )
    return CommandResult("prune", "planned" if dry_run else "pruned", removed=len(removed))


def verify_backups(
    config_path: Path,
    *,
    runtime_params: Mapping[str, str] | None = None,
    user_mode: bool = False,
    progress: Callable[[str], None] | None = None,
    on_verified: Callable[[Path], None] | None = None,
    on_progress: Callable[[VerificationProgress], None] | None = None,
) -> CommandResult:
    """Verify metadata, checksums, archive streams, credentials, and safe TAR members."""

    def report(message: str) -> None:
        if progress is not None:
            with suppress(Exception):
                progress(message)

    runtime_owner(user_mode, config_path=config_path)
    parameters = dict(runtime_params or {})
    report("Loading and validating configuration")
    config = load_validated_config(config_path, runtime_params=parameters)
    _validate_runtime(config, require_sources=False, require_writable_destination=False)
    with destination_lock(Path(config.destination.root)):
        return _verify_backups(
            config,
            parameters=parameters,
            user_mode=user_mode,
            report=report,
            on_verified=on_verified,
            on_progress=on_progress,
        )


def _verify_backups(
    config: JobConfig,
    *,
    parameters: Mapping[str, str],
    user_mode: bool,
    report: Callable[[str], None],
    on_verified: Callable[[Path], None] | None,
    on_progress: Callable[[VerificationProgress], None] | None,
) -> CommandResult:
    report("Checking destination")
    _validate_runtime(config, require_sources=False, require_writable_destination=False)
    report("Verifying backup manifests, checksums, and archive contents")
    report_progress(
        on_progress,
        VerificationProgress(
            "discovery", "Verification destination", path=Path(config.destination.root)
        ),
    )
    destinations = file_destinations(config) if config.backup_mode == "per_file" else (config,)
    owner_uid, owner_gid = runtime_owner(user_mode)
    loaded_password = (
        load_password_file(
            Path(config.encryption.password_file), owner_uid=owner_uid, owner_gid=owner_gid
        )
        if config.encryption.password_file
        else None
    )
    tools = ArchiveTools()
    count = 0
    failures: list[str] = []
    archive_failed = False
    try:
        backups: list[DiscoveredBackup] = []
        backup_total: int | None = 0
        for destination in destinations:
            try:
                discovered = discover_backups(
                    destination,
                    runtime_params=parameters,
                    metadata_only=True,
                    on_progress=on_progress,
                )
            except (DestinationError, OSError) as error:
                failures.append(f"{destination.destination.root}: {error}")
                backup_total = None
                report_progress(
                    on_progress,
                    VerificationProgress(
                        "discovery",
                        "Backup discovery",
                        "failed",
                        Path(destination.destination.root),
                        error=str(error),
                    ),
                )
                continue
            if backup_total is not None:
                backup_total += len(discovered.backups) + len(discovered.malformed)
            for backup in discovered.backups:

                def checksum_progress(
                    event: VerificationProgress, *, backup: DiscoveredBackup = backup
                ) -> None:
                    report_progress(
                        on_progress,
                        replace(
                            event,
                            backup_directory=backup.directory,
                            created_at=backup.manifest.created_at,
                            backup_id=backup.manifest.backup_id,
                        ),
                    )

                try:
                    verify_backup_checksum(backup, on_progress=checksum_progress)
                except (VaultkeepError, OSError) as error:
                    archive_failed = True
                    if isinstance(error, ArchiveCorruptionError):
                        mark_backup_suspect(backup.directory, str(error))
                    failures.append(f"{backup.directory}: SHA-256: {error}")
                    checksum_progress(
                        VerificationProgress(
                            "backup", "SHA-256", "failed", backup.archive_path, error=str(error)
                        )
                    )
                    continue
                backups.append(backup)
            failures.extend(
                f"{backup.directory}: SUSPECT: {backup.suspect_reason}"
                for backup in discovered.backups
                if backup.suspect_reason is not None
            )
            failures.extend(
                f"{entry.path}: malformed backup: {entry.reason}" for entry in discovered.malformed
            )
        report_progress(
            on_progress,
            VerificationProgress(
                "discovery",
                "SHA-256 verification complete",
                "complete",
                completed=len(backups),
                total=backup_total,
            ),
        )
        for index, backup in enumerate(backups, start=1):
            operation = "Archive verification"

            def archive_progress(
                event: VerificationProgress,
                *,
                index: int = index,
                backup: DiscoveredBackup = backup,
            ) -> None:
                nonlocal operation
                operation = event.operation
                report_progress(
                    on_progress,
                    replace(
                        event,
                        index=index,
                        total=len(backups),
                        backup_directory=backup.directory,
                        created_at=backup.manifest.created_at,
                        backup_id=backup.manifest.backup_id,
                    ),
                )

            if on_progress is None:
                report(f"Verifying archive {str(backup.archive_path)!r}")
            archive_progress(VerificationProgress("archive", operation, path=backup.archive_path))
            try:
                if backup.manifest.archive_format == "tar.zst":
                    verify_tar_zstd(backup.archive_path, tools=tools, on_progress=archive_progress)
                else:
                    if loaded_password is None:
                        raise ArchiveVerificationError("Encrypted backup requires a password")
                    verify_tar_7z(
                        backup.archive_path,
                        job_id=backup.manifest.job,
                        password=loaded_password.secret,
                        tools=tools,
                        on_progress=archive_progress,
                    )
            except (VaultkeepError, OSError) as error:
                if isinstance(error, (ArchiveChecksumMismatchError, ArchiveCorruptionError)):
                    mark_backup_suspect(backup.directory, str(error))
                archive_failed = True
                failures.append(f"{backup.archive_path}: {operation}: {error}")
                archive_progress(
                    VerificationProgress(
                        "archive", operation, "failed", backup.archive_path, error=str(error)
                    )
                )
                continue
            count += 1
            archive_progress(
                VerificationProgress("archive", "Archive verification", "ok", backup.archive_path)
            )
            if on_verified is not None:
                with suppress(Exception):
                    on_verified(backup.archive_path)
        report_progress(
            on_progress,
            VerificationProgress(
                "archive",
                "Archive verification complete",
                "complete",
                completed=count,
                total=len(backups),
            ),
        )
    finally:
        if loaded_password is not None:
            loaded_password.secret.clear()
    if failures:
        error_type = ArchiveVerificationError if archive_failed else DestinationError
        raise error_type("Backup verification failed:\n" + "\n".join(failures))
    return CommandResult("verify", "verified", backups=count)


def run_backup(
    config_path: Path,
    *,
    runtime_params: Mapping[str, str] | None = None,
    paths: WorkflowPaths | None = None,
    user_mode: bool = False,
    progress: Callable[[str], None] | None = None,
) -> CommandResult:
    """Execute change detection, archival, commit, state persistence, and retention."""
    parameters = dict(runtime_params or {})
    runtime_owner(user_mode, config_path=config_path)
    # Establish job ownership before any runtime checks or progress publication.
    config = load_config(config_path)
    paths = paths or (user_workflow_paths() if user_mode else WorkflowPaths())
    identity = job_identity_hash(config_path, config.job.id, runtime_params=parameters)
    with Execution(
        paths,
        config.job.id,
        identity,
        progress=progress,
        notifications=NotificationConfig(
            enabled=config.notifications.backup.enabled,
            command=config.notifications.command,
            timeout_ms=config.notifications.timeout_ms,
        ),
        per_file=config.backup_mode == "per_file",
    ) as execution:
        execution.report("Loading and validating configuration")
        config, application = prepare_job(
            config,
            user_mode=user_mode,
            config_path=config_path,
            runtime_params=parameters,
        )
        if application is not None:
            validate_application_directory(application, config_path=config_path)
        expected_policy = os.environ.get("VAULTKEEP_EXPECTED_POLICY")
        if expected_policy is not None and json.loads(expected_policy) != execution_policy(
            config, application
        ):
            raise SourceDiscoveryError(
                "Execution policy or application identity changed after Trigger dispatch"
            )
        if user_mode:
            _prepare_user_paths(paths)
        # Check operator-selected mount/marker guards before a network lock file
        # can be created, then repeat normal preflight under the acquired lock.
        _validate_runtime(config, require_sources=False, require_writable_destination=False)
        with destination_lock(Path(config.destination.root)):
            result = (
                _run_file_backups if config.backup_mode == "per_file" else _run_single_backup
            )(
                config_path,
                config,
                runtime_params=parameters,
                paths=paths,
                user_mode=user_mode,
                progress=execution.report,
                application=application,
            )
        execution.complete(
            result.result,
            errors=[f.error for f in result.files if f.error],
            cleanup_errors=result.cleanup_errors,
        )
        return result


def _run_file_backups(
    config_path: Path,
    config: JobConfig,
    *,
    runtime_params: Mapping[str, str] | None,
    paths: WorkflowPaths,
    user_mode: bool,
    progress: Callable[[str], None] | None,
    application: ResolvedApplication | None = None,
) -> CommandResult:
    report = progress if progress is not None else lambda message: None
    _validate_runtime(config, require_sources=False, require_writable_destination=True)
    _validate_configured_hooks(config, user_mode=user_mode)
    outcomes: list[FileBackupResult] = []
    removed = 0
    cleanup_errors: list[str] = []
    archive_size = 0
    require_application_offline(config, application=application)
    report("Discovering files for independent backups")
    for source in select_files(config):
        report(f"Backing up {source.name!r}")
        try:
            # Recheck the parent share and marker before creating each namespace.
            _validate_runtime(config, require_sources=False, require_writable_destination=False)
            child = file_config(config, source)
            require_file_directory(Path(child.destination.root), create=True)
            result = _run_single_backup(
                config_path,
                child,
                runtime_params=runtime_params,
                paths=paths,
                user_mode=user_mode,
                progress=progress,
                source_file_key=file_key(source.name),
                application=application,
            )
            outcomes.append(FileBackupResult(source, result.result, result.archive_path))
            cleanup_errors.extend(f"{source}: {error}" for error in result.cleanup_errors)
            removed += result.removed
            archive_size += result.archive_size or 0
        except (VaultkeepError, OSError) as error:
            report("Handling backup failure")
            logging.getLogger(__name__).error(
                "Backup of %s failed: %s", source, error, exc_info=error
            )
            outcomes.append(FileBackupResult(source, "failed", error=failure_reason(error)))
    if any(outcome.result == "failed" for outcome in outcomes):
        status = "failed"
    elif any(outcome.result == "created" for outcome in outcomes):
        status = "created"
    else:
        status = "unchanged"
    return CommandResult(
        "run",
        status,
        backups=sum(outcome.result == "created" for outcome in outcomes),
        removed=removed,
        archive_size=archive_size,
        files=tuple(outcomes),
        cleanup_errors=tuple(cleanup_errors),
    )


def _run_single_backup(
    config_path: Path,
    config: JobConfig,
    *,
    runtime_params: Mapping[str, str] | None = None,
    paths: WorkflowPaths,
    user_mode: bool = False,
    progress: Callable[[str], None] | None = None,
    source_file_key: str | None = None,
    application: ResolvedApplication | None = None,
) -> CommandResult:
    """Execute the same verified archive workflow for one backup unit."""
    report = progress if progress is not None else lambda message: None
    report("Preparing backup")
    owner_uid, owner_gid = runtime_owner(user_mode)
    hook_kwargs: _HookRunKwargs = (
        {"owner_uid": owner_uid, "owner_gid": owner_gid, "allow_root_owned": True}
        if user_mode
        else {}
    )
    hook_kwargs["progress"] = report
    parameters = dict(runtime_params or {})
    report("Checking sources, destination, and hooks")
    _validate_runtime(config, require_sources=False, require_writable_destination=True)
    if user_mode:
        _validate_configured_hooks(config, user_mode=True)
    else:
        _validate_configured_hooks(config)
    identity = job_identity_hash(
        config_path, config.job.id, runtime_params=parameters, file_key=source_file_key
    )
    state_path = job_state_path(
        config_path,
        config.job.id,
        runtime_params=parameters,
        state_root=paths.state_root,
        file_key=source_file_key,
    )
    hook_outcomes: list[HookOutcomeState] = []
    reconciliation = None
    latest_state: LocalState | None = None
    loaded_password = None
    base_hook_context = HookContext(
        config.job.id,
        config_path,
        hostname=socket.gethostname(),
        archive_format=config.archive.format,
    )
    failure_context = replace(base_hook_context, destination=config.destination.root)
    terminal_hook_failed = False
    staging_path: Path | None = None
    release_armed = False
    release_attempted = False

    try:
        release_armed = True
        _run_configured_hook(
            "before_check",
            config,
            config_path,
            base_hook_context,
            hook_outcomes,
            **hook_kwargs,
        )
        require_application_offline(config, application=application)
        report("Discovering source files")
        if source_file_key is not None:
            source = Path(config.sources[0].path)
            if not stat.S_ISREG(source.lstat().st_mode):
                raise SourceDiscoveryError(f"Selected source is no longer a regular file: {source}")
        snapshot = discover_sources(config)
        if source_file_key is not None and (
            len(snapshot.entries) != 1 or snapshot.entries[0].entry_type is not SourceEntryType.FILE
        ):
            raise SourceDiscoveryError("Selected source is no longer a single regular file")
        report("Hashing source files")
        source_digest = calculate_source_digest(snapshot)
        failure_context = replace(failure_context, source_digest=source_digest)
        config_fingerprint = calculate_config_fingerprint(
            config, application=application, runtime_params=parameters
        )
        report("Checking existing backups")
        discovered = _active_history(
            discover_backups(config, runtime_params=parameters, metadata_only=True),
            config,
            application=application,
        )
        if any(entry.unsupported_format for entry in discovered.malformed):
            raise DestinationError(
                "Unsupported destination format; use a fresh schema-2 destination "
                "and preserve this history separately"
            )
        newest = max(
            (backup for backup in discovered.backups if backup.suspect_reason is None),
            key=lambda backup: (backup.manifest.created_datetime_utc, backup.manifest.backup_id),
            default=None,
        )
        replacement_required = any(
            backup.suspect_reason is not None and valid_replacement(backup, discovered) is None
            for backup in discovered.backups
        )
        if replacement_required:
            report("Unresolved suspect needs replacement; creating fresh backup")
        if (
            newest is not None
            and not replacement_required
            and newest.manifest.source_digest == source_digest
            and newest.manifest.config_fingerprint == config_fingerprint
        ):
            report("Verifying latest backup checksum")
            try:
                verify_backup_checksum(newest)
            except ArchiveChecksumMismatchError as error:
                report("Latest backup suspect; creating replacement")
                mark_backup_suspect(newest.directory, str(error))
                logging.getLogger(__name__).warning(
                    "Backup %s is suspect and will be preserved; creating a fresh backup: %s",
                    newest.archive_path,
                    error,
                )
                replacement_required = True
                discovered = replace(
                    discovered,
                    backups=tuple(
                        replace(backup, suspect_reason=str(error)) if backup == newest else backup
                        for backup in discovered.backups
                    ),
                )
            except ArchiveVerificationError as error:
                raise ArchiveVerificationError(
                    f"Latest backup checksum verification failed for {newest.archive_path}: {error}"
                ) from error
        if not replacement_required:
            for suspect in discovered.backups:
                replacement = valid_replacement(suspect, discovered)
                if replacement is not None:
                    try:
                        verify_backup_checksum(replacement)
                    except ArchiveChecksumMismatchError as error:
                        mark_backup_suspect(replacement.directory, str(error))
                        replacement_required = True
                        discovered = _active_history(
                            discover_backups(config, runtime_params=parameters, metadata_only=True),
                            config,
                            application=application,
                        )
                        break
        loaded_password = (
            load_password_file(
                Path(config.encryption.password_file),
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
            if config.encryption.password_file
            else None
        )
        report("Checking backup state and source changes")
        reconciliation = reconcile_local_state(
            state_path,
            job_id=config.job.id,
            identity_hash=identity,
            application_version=installed_version(),
            destination_backups=discovered.state_records,
            current_credential=loaded_password.fingerprint if loaded_password else None,
            verify_credential=(
                encrypted_backup_credential_verifier(loaded_password.secret, tools=ArchiveTools())
                if loaded_password
                else None
            ),
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
        failure_context = replace(failure_context, timestamp=now)
        if decision.unchanged and not replacement_required:
            previous = reconciliation.state.last_successful_backup
            if previous is None:
                raise AssertionError("Unchanged state has no successful backup")
            report("Sources unchanged; skipping archive creation")
            terminal_hook_failed = True
            _run_configured_hook(
                "on_unchanged",
                config,
                config_path,
                replace(
                    failure_context,
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
            report("Saving backup state")
            write_local_state(
                state_path,
                latest_state,
            )
            return CommandResult("run", "unchanged", archive_path=Path(previous.backup_path))
        report("Preparing archive destination")
        backup_id = uuid.uuid4().hex
        allocated = allocate_job_backup_paths(
            config.destination,
            job_id=config.job.id,
            backup_id=backup_id,
            hostname=base_hook_context.hostname,
            created_at=now,
            source_digest=source_digest,
            archive_format=config.archive.format,
            runtime_params=parameters,
        )
        create_staging_directory(allocated)
        staging_path = allocated.staging_directory
        hook_context = replace(
            failure_context,
            backup_id=backup_id,
            archive=str(allocated.final_directory / allocated.archive_path.name),
            backup_directory=str(allocated.final_directory),
            version=installed_version(),
        )
        failure_context = hook_context
        try:
            _run_configured_hook(
                "before_archive",
                config,
                config_path,
                hook_context,
                hook_outcomes,
                **hook_kwargs,
            )
            require_application_offline(config, application=application)
            snapshot = discover_sources(config)
            if source_file_key is not None and (
                len(snapshot.entries) != 1
                or snapshot.entries[0].entry_type is not SourceEntryType.FILE
            ):
                raise SourceDiscoveryError("Selected source is no longer a single regular file")
            source_digest = calculate_source_digest(snapshot)
            allocated = allocate_job_backup_paths(
                config.destination,
                job_id=config.job.id,
                backup_id=backup_id,
                hostname=base_hook_context.hostname,
                created_at=now,
                source_digest=source_digest,
                archive_format=config.archive.format,
                runtime_params=parameters,
            )
            hook_context = replace(
                hook_context,
                source_digest=source_digest,
                archive=str(allocated.final_directory / allocated.archive_path.name),
                backup_directory=str(allocated.final_directory),
            )
            failure_context = hook_context
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
                progress=report,
            )
            require_application_offline(config, application=application)
        except BaseException:
            raise
        release_attempted = True
        _run_configured_hook(
            "after_archive", config, config_path, hook_context, hook_outcomes, **hook_kwargs
        )
        # The release hook may restart the app. Only backup artifacts are read now.
        for staged_artifact in (
            allocated.staging_directory,
            allocated.archive_path,
            allocated.checksum_path,
        ):
            mode = staged_artifact.lstat().st_mode
            if not (
                stat.S_ISDIR(mode)
                if staged_artifact == allocated.staging_directory
                else stat.S_ISREG(mode)
            ):
                raise DestinationError(
                    f"Release hook changed staging artifact type: {staged_artifact}"
                )
        if {path.name for path in allocated.staging_directory.iterdir()} != {
            allocated.archive_path.name,
            allocated.checksum_path.name,
        }:
            raise DestinationError("Release hook left unexpected staging artifacts")
        staged_digest = verify_checksum_sidecar(allocated.archive_path, allocated.checksum_path)
        if staged_digest != artifact.sha256:
            raise ArchiveChecksumMismatchError("Release hook changed staged archive artifacts")
        report("Finalizing backup")
        manifest = commit_archive_artifact(
            allocated,
            artifact,
            application_version=installed_version(),
            job_id=config.job.id,
            hostname=base_hook_context.hostname,
            created_at=now,
            config_fingerprint=config_fingerprint,
            replacement_identity=replacement_identity(config, application=application),
        )
        staging_path = None
        report("Checking finalized backup")
        committed = _active_history(
            discover_backups(config, runtime_params=parameters, metadata_only=True),
            config,
            application=application,
        )
        candidates = tuple(
            backup
            for backup in committed.backups
            if backup.manifest.backup_id == manifest.backup_id
        )
        if len(candidates) != 1:
            raise DestinationError("Committed archive cannot be rediscovered unambiguously")
        created = candidates[0]
        record = created.state_record
        try:
            verify_backup_checksum(created)
        except ArchiveChecksumMismatchError as error:
            mark_backup_suspect(created.directory, str(error))
            raise
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
        report("Saving backup state")
        write_local_state(
            state_path,
            latest_state,
        )
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
        report("Saving backup state")
        write_local_state(state_path, latest_state)
        committed = _active_history(
            discover_backups(config, runtime_params=parameters, metadata_only=True),
            config,
            application=application,
        )
        candidates = tuple(
            backup for backup in committed.backups if backup.manifest.backup_id == record.backup_id
        )
        if len(candidates) != 1 or candidates[0].suspect_reason is not None:
            raise DestinationError(
                "Newly committed backup is ambiguous, unavailable or SUSPECT after success hooks"
            )
        current = candidates[0]
        try:
            verify_backup_checksum(current)
        except ArchiveChecksumMismatchError as error:
            mark_backup_suspect(current.directory, str(error))
            raise
        cleanup_errors: tuple[str, ...] = ()
        removed: tuple[Path, ...] = ()
        try:
            if committed.malformed:
                raise PruneError("Matching malformed destination entries block cleanup")
            for backup in committed.backups:
                if (
                    backup.suspect_reason is not None
                    and valid_replacement(backup, committed) is None
                ):
                    record_suspect_replacement(backup.directory, record.backup_id)
            committed = _active_history(
                discover_backups(config, runtime_params=parameters, metadata_only=True),
                config,
                application=application,
            )
            report("Applying retention")
            plan = build_prune_plan(committed, config.retention)
            if any(
                backup.manifest.backup_id == record.backup_id for backup in plan.backups_to_delete
            ):
                raise PruneError(
                    "Retention ordering would delete the newly created backup; "
                    "preserving the history for inspection"
                )
            removed = execute_prune_plan(plan, committed)
        except (VaultkeepError, OSError) as cleanup_error:
            if (
                isinstance(cleanup_error, PruneError)
                and record.backup_id in cleanup_error.corrupt_backup_ids
            ):
                raise ArchiveChecksumMismatchError(
                    "Newly committed backup failed integrity checks during cleanup: "
                    f"{cleanup_error}"
                ) from cleanup_error
            if isinstance(cleanup_error, PruneError):
                removed = tuple(Path(path) for path in cleanup_error.removed)
            cleanup_errors = (str(cleanup_error),)
            logging.getLogger(__name__).warning(
                "Backup created; cleanup failed: %s. Retry with prune or a later new backup.",
                cleanup_error,
            )
        return CommandResult(
            "run",
            "created",
            removed=len(removed),
            archive_path=Path(record.backup_path),
            archive_size=artifact.size,
            cleanup_errors=cleanup_errors,
        )
    except BaseException as error:
        if release_armed and not release_attempted:
            release_attempted = True
            try:
                _run_configured_hook(
                    "after_archive",
                    config,
                    config_path,
                    failure_context,
                    hook_outcomes,
                    **hook_kwargs,
                )
            except BaseException as release_error:
                error.add_note(f"after_archive also failed: {release_error}")
                logging.getLogger(__name__).error("Release hook also failed: %s", release_error)
        report("Handling backup failure")
        if staging_path is not None:
            try:
                shutil.rmtree(staging_path)
            except FileNotFoundError:
                pass  # The directory may already have been renamed at the commit point.
            except OSError as cleanup_error:
                logging.getLogger(__name__).warning(
                    "Cannot remove partial backup directory %s: %s", staging_path, cleanup_error
                )
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


def _validate_runtime(
    config: JobConfig, *, require_sources: bool, require_writable_destination: bool
) -> None:
    root = Path(config.destination.root)
    if not root.is_dir():
        raise DestinationError(f"Destination root is not an accessible directory: {root}")
    if config.destination.expected_fstype is not None:
        actual_fstype = _mounted_filesystem_type(root)
        if actual_fstype != config.destination.expected_fstype:
            raise DestinationError(
                f"Destination filesystem type mismatch at {root}: "
                f"expected {config.destination.expected_fstype}, found {actual_fstype}"
            )
    if (
        config.destination.marker_file is not None
        and not (root / config.destination.marker_file).is_file()
    ):
        raise DestinationError("Configured destination marker is missing")
    if require_writable_destination:
        probe_destination_writability(root)
    if require_sources:
        for source in config.sources:
            if not Path(source.path).exists() and not config.source_options.ignore_missing:
                raise DestinationError(f"Configured source does not exist: {source.path}")


def _mounted_filesystem_type(path: Path) -> str:
    """Inspect this process's mount namespace, including bind mounts, without writing."""
    command = [
        "findmnt",
        "--kernel",
        "--uniq",
        "--noheadings",
        "--raw",
        "--output",
        "FSTYPE",
        "--target",
        str(path),
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
        raise DestinationError(
            f"Cannot inspect destination filesystem at {path}: {error}"
        ) from error
    filesystem_types = completed.stdout.split()
    if completed.returncode != 0 or len(filesystem_types) != 1:
        raise DestinationError(
            f"Cannot inspect destination filesystem at {path}: "
            "findmnt did not return one mounted filesystem type"
        )
    return filesystem_types[0]


def _prepare_user_paths(paths: WorkflowPaths) -> None:
    for path in (paths.state_root, paths.local_temp_root, paths.lock_root):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(path, 0o700)


def _validate_configured_hooks(config: JobConfig, *, user_mode: bool = False) -> None:
    owner_uid, owner_gid = runtime_owner(user_mode)
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
            validate_hook_shell(
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
    progress: Callable[[str], None] | None = None,
) -> None:
    del config_path
    hook = getattr(config.hooks, phase)
    if hook is not None:
        if progress is not None:
            progress(f"Running {phase} hook")
        execution = run_hook(
            phase,
            hook,
            context,
            include_command_output=config.logging.include_command_output,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            allow_root_owned=allow_root_owned,
        )
        outcomes.append(execution.outcome)
        require_success(execution)


def _active_history(
    discovered: DiscoveryResult, config: JobConfig, *, application: ResolvedApplication | None
) -> DiscoveryResult:
    identity = replacement_identity(config, application=application)
    seen: set[str] = set()
    duplicate_entries: list[MalformedDestinationEntry] = []
    for backup in discovered.backups:
        if backup.manifest.backup_id in seen:
            duplicate_entries.append(
                MalformedDestinationEntry(
                    backup.directory, "Duplicate backup identities block cleanup"
                )
            )
        seen.add(backup.manifest.backup_id)

    def compatible(backup: DiscoveredBackup) -> bool:
        if backup.manifest.replacement_identity == identity:
            return True
        if config.backup_mode != "per_file":
            return False
        # Parent inventory/prune must resolve each file's coverage without requiring
        # source availability. Deleted/excluded/changed source histories stay intact.
        from vaultkeep.sources.exclusions import compile_exclusions

        try:
            scope = json.loads(backup.manifest.replacement_identity.source_scope)
            source_path = Path(scope["sources"][0]["path"])
            parent = Path(config.sources[0].path)
            matcher = compile_exclusions((*config.exclude, *config.sources[0].exclude))
            if (
                source_path.parent != parent
                or backup.directory.parent.name != file_key(source_path.name)
                or matcher.matches(source_path.name, is_directory=False)
            ):
                return False
            return backup.manifest.replacement_identity == replacement_identity(
                file_config(config, source_path), application=application
            )
        except (ValueError, KeyError, IndexError, TypeError):
            return False

    return DiscoveryResult(
        tuple(backup for backup in discovered.backups if compatible(backup)),
        discovered.malformed + tuple(duplicate_entries),
    )
