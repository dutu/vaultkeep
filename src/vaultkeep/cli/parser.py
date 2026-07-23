"""Argument parser construction."""

from __future__ import annotations

import argparse


def create_parser() -> argparse.ArgumentParser:
    """Create the top-level Vaultkeep argument parser."""
    parser = argparse.ArgumentParser(prog="vaultkeep")
    parser.add_argument(
        "--version",
        action="store_true",
        dest="show_version",
        help="print the installed version and exit",
    )
    return parser
