"""Top-level command dispatch."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from contextlib import suppress
from time import monotonic

from vaultkeep.cli.parser import create_parser
from vaultkeep.cli.progress import terminal_progress
from vaultkeep.config import parse_runtime_parameters
from vaultkeep.errors import (
    ConfigurationError,
    DestinationError,
    HookError,
    LockError,
    StateError,
    TimerError,
    VaultkeepError,
)
from vaultkeep.scheduling import TimerManager
from vaultkeep.version import installed_version
from vaultkeep.workflow import (
    CommandResult,
    list_backups,
    prune_backups,
    run_backup,
    validate_job,
    verify_backups,
)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Vaultkeep command-line interface."""
    arguments = list(argv) if argv is not None else sys.argv[1:]
    parser = create_parser()
    namespace = parser.parse_args(arguments)

    if namespace.show_version:
        print(installed_version())
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
        print(error, file=sys.stderr)
        return 3
    try:
        if namespace.command == "validate":
            if not namespace.backup_only:
                from vaultkeep.cli.validation import validate_command

                return validate_command(
                    namespace.config,
                    schema_only=namespace.schema_only,
                    runtime_params=runtime_params,
                    user_mode=namespace.user_mode,
                )
            result = validate_job(
                namespace.config, schema_only=namespace.schema_only, runtime_params=runtime_params
            )
        elif namespace.command == "run":
            started = monotonic()
            with terminal_progress() as progress:
                result = run_backup(
                    namespace.config,
                    runtime_params=runtime_params,
                    user_mode=namespace.user_mode,
                    progress=progress,
                )
            elapsed = monotonic() - started
        elif namespace.command == "list":
            result, _ = list_backups(namespace.config, runtime_params=runtime_params)
        elif namespace.command == "verify":
            with terminal_progress() as progress:
                result = verify_backups(
                    namespace.config, runtime_params=runtime_params, progress=progress
                )
        elif namespace.command == "prune":
            result = prune_backups(
                namespace.config, dry_run=namespace.dry_run, runtime_params=runtime_params
            )
        else:
            manager = TimerManager(user_mode=namespace.user_mode)
            manager.require_environment()
            if namespace.command == "schedule":
                if namespace.action == "install":
                    manager.install(namespace.config, runtime_params=runtime_params)
                elif namespace.action == "update":
                    manager.update(namespace.config, runtime_params=runtime_params)
                elif namespace.action == "status":
                    print(manager.status(namespace.config, runtime_params=runtime_params))
                elif namespace.action == "next":
                    print(manager.next(namespace.config, runtime_params=runtime_params))
                elif namespace.action == "disable":
                    manager.disable(namespace.config, runtime_params=runtime_params)
                else:
                    manager.remove(namespace.config, runtime_params=runtime_params)
                return 0
            if namespace.action == "list":
                print("\n".join(manager.list()))
            elif namespace.action == "validate":
                for item in manager.validate_all():
                    print(item)
            else:
                print("\n".join(manager.sync(dry_run=namespace.dry_run)))
            return 0
    except ConfigurationError as error:
        print(error, file=sys.stderr)
        return 3
    except StateError as error:
        print(error, file=sys.stderr)
        return 15
    except DestinationError as error:
        print(error, file=sys.stderr)
        return 5
    except HookError as error:
        print(error, file=sys.stderr)
        return 11
    except LockError as error:
        print(error, file=sys.stderr)
        return 10
    except TimerError as error:
        print(error, file=sys.stderr)
        return 14
    except VaultkeepError as error:
        print(error, file=sys.stderr)
        return 7
    print(f"{result.command}: {result.result}", flush=True)
    if result.files:
        _print_file_results(result)
    if namespace.command == "run" and result.result == "created" and progress is not None:
        _print_run_summary(result, elapsed)
    return 7 if result.result == "failed" else 0


def _print_file_results(result: CommandResult) -> None:
    """Keep per-file diagnostics on stderr and stdout's Trigger marker unchanged."""
    with suppress(OSError):
        for outcome in result.files:
            print(f"{outcome.source.name!r}: {outcome.result}", file=sys.stderr)
            if outcome.archive_path is not None and outcome.result == "created":
                print(f"  Archive: {outcome.archive_path}", file=sys.stderr)
            if outcome.error is not None:
                print(f"  {outcome.error}", file=sys.stderr)


def _print_run_summary(result: CommandResult, elapsed: float) -> None:
    """Present completed backup facts on the terminal without rereading the archive."""
    lines = []
    if result.archive_path is not None:
        lines.append(f"Archive: {result.archive_path}")
    if result.archive_size is not None:
        lines.append(f"Size: {_format_size(result.archive_size)}")
    lines.append(f"Duration: {elapsed:.1f}s")
    lines.append(f"Backups removed by retention: {result.removed}")
    # Losing the summary stream must not turn a completed backup into a failure.
    with suppress(OSError):
        print("\n".join(lines), file=sys.stderr, flush=True)


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{size} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("Unreachable size unit")
