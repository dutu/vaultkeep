"""Vaultkeep command-line interface with deployment-safe lazy imports."""

from __future__ import annotations

from collections.abc import Sequence

from vaultkeep.cli.parser import create_parser
from vaultkeep.errors import LockError
from vaultkeep.locking import deployment_lock, requires_deployment_lock

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    """Take the deployment lock before importing the backup/monitor workflow."""
    namespace = create_parser().parse_args(argv)
    guarded = not namespace.show_version and requires_deployment_lock(
        namespace.command, getattr(namespace, "action", None)
    )
    if guarded:
        try:
            with deployment_lock():
                from vaultkeep.cli.commands import main as execute

                return execute(argv)
        except LockError as error:
            from vaultkeep.cli.output import Console

            Console(no_color=namespace.no_color).issue(error)
            return 3 if namespace.command == "trigger" else 10
    from vaultkeep.cli.commands import main as execute

    return execute(argv)
