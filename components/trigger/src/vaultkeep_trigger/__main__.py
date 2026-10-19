"""Module entry point for ``python -m vaultkeep_trigger``."""

from __future__ import annotations

import sys

from vaultkeep_trigger.cli import main

if __name__ == "__main__":
    sys.exit(main())
