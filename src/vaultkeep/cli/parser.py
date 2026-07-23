"""Argument parser construction."""

from __future__ import annotations

import argparse
from pathlib import Path


def create_parser() -> argparse.ArgumentParser:
    """Create the top-level Vaultkeep argument parser."""
    parser = argparse.ArgumentParser(prog="vaultkeep")
    parser.add_argument(
        "--version",
        action="store_true",
        dest="show_version",
        help="print the installed version and exit",
    )
    parser.add_argument("--config", type=Path, help="path to one job YAML configuration")
    commands = parser.add_subparsers(dest="command")
    validate = commands.add_parser("validate")
    validate.add_argument("--schema-only", action="store_true")
    commands.add_parser("run")
    commands.add_parser("list")
    commands.add_parser("verify")
    prune = commands.add_parser("prune")
    prune.add_argument("--dry-run", action="store_true")
    return parser
