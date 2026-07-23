"""Top-level command dispatch."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from vaultkeep.cli.parser import create_parser
from vaultkeep.errors import ConfigurationError, DestinationError, StateError, VaultkeepError
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

    if namespace.command is None or namespace.config is None:
        parser.error("--config and a command are required")
    try:
        if namespace.command == "validate":
            result = validate_job(namespace.config, schema_only=namespace.schema_only)
        elif namespace.command == "run":
            result = run_backup(namespace.config)
        elif namespace.command == "list":
            result, _ = list_backups(namespace.config)
        elif namespace.command == "verify":
            result = verify_backups(namespace.config)
        else:
            result = prune_backups(namespace.config, dry_run=namespace.dry_run)
    except ConfigurationError as error:
        print(error, file=sys.stderr)
        return 3
    except StateError as error:
        print(error, file=sys.stderr)
        return 15
    except DestinationError as error:
        print(error, file=sys.stderr)
        return 5
    except VaultkeepError as error:
        print(error, file=sys.stderr)
        return 7
    print(f"{result.command}: {result.result}")
    return 0
