"""Top-level command dispatch."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from vaultkeep.cli.parser import create_parser
from vaultkeep.version import installed_version


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Vaultkeep command-line interface."""
    arguments = list(argv) if argv is not None else sys.argv[1:]
    parser = create_parser()
    namespace = parser.parse_args(arguments)

    if namespace.show_version:
        print(installed_version())
        return 0

    parser.error("a command is required")
    return 2
