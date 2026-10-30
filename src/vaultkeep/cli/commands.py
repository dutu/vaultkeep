"""Top-level command dispatch."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from time import monotonic

from vaultkeep.backup.results import BackupInterrupted, failure_exit_code
from vaultkeep.backup.workflow import (
    CommandResult,
    list_backups,
    prune_backups,
    run_backup,
    validate_job,
    verify_backups,
)
from vaultkeep.cli.inventory import print_inventory
from vaultkeep.cli.output import Console, command_logging, configure_logging, format_size
from vaultkeep.cli.parser import create_parser
from vaultkeep.cli.progress import CliProgress, VerificationDisplay
from vaultkeep.config import load_config, parse_runtime_parameters
from vaultkeep.errors import (
    ApplicationIdentityError,
    ConfigurationError,
    LockError,
    ValidationIssue,
    VaultkeepError,
)
from vaultkeep.locking import deployment_lock, requires_deployment_lock
from vaultkeep.schedule import TimerManager
from vaultkeep.version import installed_version


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Vaultkeep command-line interface."""
    with command_logging():
        return _main(argv)


def _main(argv: Sequence[str] | None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    parser = create_parser()
    namespace = parser.parse_args(arguments)
    output = Console(no_color=namespace.no_color)

    if namespace.show_version:
        print(installed_version())
        return 0

    guarded = requires_deployment_lock(namespace.command, getattr(namespace, "action", None))
    try:
        if guarded:
            with deployment_lock():
                return _dispatch(namespace, parser, output)
        return _dispatch(namespace, parser, output)
    except LockError as error:
        output.issue(error)
        return 3 if namespace.command == "trigger" else 10


def _dispatch(
    namespace: argparse.Namespace, parser: argparse.ArgumentParser, output: Console
) -> int:

    if namespace.command == "notify":
        from vaultkeep.application import runtime_owner
        from vaultkeep.notifications.external import notify
        from vaultkeep.validation import validate_template_semantics

        configure_logging("warning", no_color=namespace.no_color)
        try:
            inherited_path = os.environ.get("VAULTKEEP_CONFIG")
            config_path = namespace.config or (Path(inherited_path) if inherited_path else None)
            if config_path is None:
                raise ConfigurationError(
                    (
                        ValidationIssue(
                            (), "notify requires --config or VAULTKEEP_CONFIG.", "config_required"
                        ),
                    )
                )
            runtime_owner(
                namespace.user_mode or (bool(inherited_path) and os.geteuid() != 0),
                config_path=config_path,
            )
            parameters = parse_runtime_parameters(namespace.param)
            config = load_config(config_path)
            validate_template_semantics(config, config_path=config_path, runtime_params=parameters)
            if config.external_notifier is None:
                raise ConfigurationError(
                    (
                        ValidationIssue(
                            ("external_notifier",),
                            "external_notifier is required by vaultkeep notify.",
                            "external_notifier_required",
                        ),
                    )
                )
            notify(config.external_notifier, namespace.arguments)
        except (ConfigurationError, ApplicationIdentityError) as error:
            output.issue(error)
            return 3
        return 0

    if namespace.command == "trigger":
        from vaultkeep.trigger.cli import run

        return run(namespace, parser)

    if namespace.command is None:
        parser.error("a command is required")
    if namespace.command != "schedules" and namespace.config is None:
        parser.error("--config is required for this command")
    if namespace.command == "schedules" and namespace.param:
        parser.error("--param requires --config")
    try:
        runtime_params = parse_runtime_parameters(namespace.param)
    except ConfigurationError as error:
        output.issue(error)
        return 3
    try:
        if namespace.command == "status":
            from vaultkeep.cli.status import status_command

            return status_command(
                namespace.config,
                user_mode=namespace.user_mode,
                runtime_params=runtime_params,
                json_output=namespace.json,
                output=output,
                verbose=namespace.verbose,
            )
        if namespace.command == "validate":
            if not namespace.backup_only:
                from vaultkeep.cli.validation import validate_command

                return validate_command(
                    namespace.config,
                    schema_only=namespace.schema_only,
                    runtime_params=runtime_params,
                    user_mode=namespace.user_mode,
                    no_color=namespace.no_color,
                )
            result = validate_job(
                namespace.config,
                schema_only=namespace.schema_only,
                runtime_params=runtime_params,
                user_mode=namespace.user_mode,
            )
        elif namespace.command == "run":
            started = monotonic()
            with CliProgress(keep_completed=True) as progress:
                result = run_backup(
                    namespace.config,
                    runtime_params=runtime_params,
                    user_mode=namespace.user_mode,
                    progress=progress.update,
                )
                if result.result == "failed":
                    progress.fail("Backup failed")
                else:
                    progress.succeed(
                        "Backup unchanged" if result.result == "unchanged" else "Backup complete"
                    )
            elapsed = monotonic() - started
        elif namespace.command == "list":
            inventory = list_backups(
                namespace.config, runtime_params=runtime_params, user_mode=namespace.user_mode
            )
            print_inventory(inventory, output=output)
            return 0
        elif namespace.command == "verify":
            with CliProgress(animate=sys.stdout.isatty()) as verification_progress:
                display = VerificationDisplay(verification_progress, verbose=namespace.verbose)
                try:
                    result = verify_backups(
                        namespace.config,
                        runtime_params=runtime_params,
                        user_mode=namespace.user_mode,
                        progress=display.stage,
                        on_progress=display.report,
                    )
                except (VaultkeepError, OSError) as error:
                    with suppress(Exception):
                        display.summary(success=False)
                    Console(no_color=namespace.no_color or not sys.stdout.isatty()).issue(error)
                    return failure_exit_code(error)
                except BaseException as error:
                    with suppress(Exception):
                        display.summary(success=False, error=error)
                    raise
                with suppress(Exception):
                    display.summary(success=result.result != "failed")
            return 7 if result.result == "failed" else 0
        elif namespace.command == "prune":
            result = prune_backups(
                namespace.config,
                dry_run=namespace.dry_run,
                runtime_params=runtime_params,
                user_mode=namespace.user_mode,
            )
        else:
            manager = TimerManager(user_mode=namespace.user_mode)
            manager.require_environment()
            if namespace.command == "schedule":
                if namespace.action == "install":
                    manager.install(namespace.config, runtime_params=runtime_params)
                elif namespace.action == "update":
                    manager.update(namespace.config, runtime_params=runtime_params)
                elif namespace.action == "next":
                    print(manager.next(namespace.config, runtime_params=runtime_params))
                elif namespace.action == "disable":
                    manager.disable(namespace.config, runtime_params=runtime_params)
                else:
                    manager.remove(namespace.config, runtime_params=runtime_params)
                if namespace.action in ("install", "update"):
                    output.print(f"schedule {namespace.action}: completed")
                return 0
            if namespace.action == "list":
                print("\n".join(manager.list()))
            elif namespace.action == "validate":
                for item in manager.validate_all():
                    output.print(item)
            else:
                print("\n".join(manager.sync(dry_run=namespace.dry_run)))
            return 0
    except (VaultkeepError, OSError, BackupInterrupted) as error:
        output.issue(error)
        return failure_exit_code(error)
    print(f"{result.command}: {result.result}", flush=True)
    for cleanup_error in result.cleanup_errors:
        output.print(
            f"Warning: backup created; cleanup failed: {cleanup_error}. "
            "Retry with prune or a later new backup.",
            file=sys.stderr,
        )
    if result.files:
        _print_file_results(result, output)
    if namespace.command == "run" and result.result == "created" and sys.stderr.isatty():
        _print_run_summary(result, elapsed, output)
    return 7 if result.result == "failed" else 0


def _print_file_results(result: CommandResult, output: Console) -> None:
    """Keep per-file diagnostics on stderr and stdout's Trigger marker unchanged."""
    with suppress(OSError):
        for outcome in result.files:
            output.print(f"{outcome.source.name!r}: {outcome.result}", file=sys.stderr)
            if outcome.archive_path is not None and outcome.result == "created":
                output.print(f"  Archive: {outcome.archive_path}", file=sys.stderr)
            if outcome.error is not None:
                output.issue(outcome.error)


def _print_run_summary(result: CommandResult, elapsed: float, output: Console) -> None:
    """Present completed backup facts on the terminal without rereading the archive."""
    rows = []
    if result.archive_path is not None:
        rows.append(("Archive", str(result.archive_path)))
    if result.archive_size is not None:
        rows.append(("Size", format_size(result.archive_size)))
    rows.append(("Duration", f"{elapsed:.1f}s"))
    rows.append(("Backups removed by retention", str(result.removed)))
    # Losing the summary stream must not turn a completed backup into a failure.
    with suppress(OSError):
        output.fields(rows, file=sys.stderr)
