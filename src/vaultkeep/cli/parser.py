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
    parser.add_argument(
        "--user",
        action="store_true",
        dest="user_mode",
        help="use per-user state, locks, hooks, secrets, and systemd user timers",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="runtime template parameter for shared configurations; repeatable",
    )
    commands = parser.add_subparsers(dest="command")
    validate = commands.add_parser("validate")
    validate.add_argument("--schema-only", action="store_true")
    commands.add_parser("run")
    commands.add_parser("list")
    commands.add_parser("verify")
    prune = commands.add_parser("prune")
    prune.add_argument("--dry-run", action="store_true")
    timer = commands.add_parser("timer")
    timer.add_argument(
        "action", choices=("install", "update", "status", "next", "disable", "remove")
    )
    timers = commands.add_parser("timers")
    timers.add_argument("action", choices=("list", "sync", "validate"))
    timers.add_argument("--dry-run", action="store_true")
    return parser
