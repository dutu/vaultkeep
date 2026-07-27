"""Top-level command dispatch."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from vaultkeep.cli.parser import create_parser
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
from vaultkeep.workflow import list_backups, prune_backups, run_backup, validate_job, verify_backups


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Vaultkeep command-line interface."""
    arguments = list(argv) if argv is not None else sys.argv[1:]
    parser = create_parser()
    namespace = parser.parse_args(arguments)

    if namespace.show_version:
        print(installed_version())
        return 0

    if namespace.command is None:
        parser.error("a command is required")
    if namespace.command != "timers" and namespace.config is None:
        parser.error("--config is required for this command")
    if namespace.command == "timers" and namespace.param:
        parser.error("--param requires --config")
    try:
        runtime_params = parse_runtime_parameters(namespace.param)
    except ConfigurationError as error:
        print(error, file=sys.stderr)
        return 3
    try:
        if namespace.command == "validate":
            result = validate_job(
                namespace.config, schema_only=namespace.schema_only, runtime_params=runtime_params
            )
        elif namespace.command == "run":
            result = run_backup(namespace.config, runtime_params=runtime_params)
        elif namespace.command == "list":
            result, _ = list_backups(namespace.config, runtime_params=runtime_params)
        elif namespace.command == "verify":
            result = verify_backups(namespace.config, runtime_params=runtime_params)
        elif namespace.command == "prune":
            result = prune_backups(
                namespace.config, dry_run=namespace.dry_run, runtime_params=runtime_params
            )
        else:
            manager = TimerManager()
            manager.require_environment()
            if namespace.command == "timer":
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
    print(f"{result.command}: {result.result}")
    return 0
