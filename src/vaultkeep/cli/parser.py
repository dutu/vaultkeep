"""Argument parser construction."""

from __future__ import annotations

import argparse
from pathlib import Path

from vaultkeep.trigger.parser import configure_parser


def create_parser() -> argparse.ArgumentParser:
    """Create the top-level Vaultkeep argument parser."""
    parser = argparse.ArgumentParser(
        prog="vaultkeep",
        description="Run backups manually, on a schedule, or after application shutdown.",
        epilog="Put job options (--user, --config, --param) before the command.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        dest="show_version",
        help="print the installed version and exit",
    )
    parser.add_argument("--no-color", action="store_true", help="disable colored terminal messages")
    parser.add_argument("--config", type=Path, help="path to one job YAML configuration")
    parser.add_argument(
        "--user",
        action="store_true",
        dest="user_mode",
        help="use per-user state, locks, hooks, secrets, and systemd user services/timers",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="runtime template parameter for shared configurations; repeatable",
    )
    commands = parser.add_subparsers(dest="command")
    configure_parser(
        commands.add_parser("trigger", help="manage this job's shutdown-triggered backups")
    )
    validate = commands.add_parser("validate", help="validate this job and check automation health")
    validate.add_argument(
        "--schema-only",
        action="store_true",
        help="check configuration structure without runtime checks",
    )
    # Monitor/install preflight already checks its own runtime prerequisites. Keep
    # its bounded child validation independent of service installation and health.
    validate.add_argument("--backup-only", action="store_true", help=argparse.SUPPRESS)
    status = commands.add_parser("status", help="report backup, Trigger, and Schedule status")
    status.add_argument("--json", action="store_true", help="report job status as JSON")
    status.add_argument(
        "--verbose", action="store_true", help="include detailed safety and execution diagnostics"
    )
    commands.add_parser("run", help="run this job's backup now")
    commands.add_parser("list", help="list this job's backups and preview retention")
    verify = commands.add_parser("verify", help="verify this job's stored backups")
    verify.add_argument(
        "--verbose",
        action="store_true",
        help="include full paths and individual verification checks",
    )
    prune = commands.add_parser("prune", help="apply this job's retention policy")
    prune.add_argument("--dry-run", action="store_true")
    schedule = commands.add_parser("schedule", help="manage this job's scheduled backups")
    actions = schedule.add_subparsers(dest="action", required=True)
    for action, help_text in (
        ("install", "install and enable this job's timer"),
        ("update", "refresh this job's installed timer"),
        ("next", "show the next scheduled run"),
        ("disable", "stop and disable this job's timer"),
        ("remove", "remove this job's timer; preserve backups and state"),
    ):
        actions.add_parser(action, help=help_text)
    schedules = commands.add_parser("schedules", help="manage schedules across all jobs")
    schedules.add_argument("action", choices=("list", "sync", "validate"))
    schedules.add_argument("--dry-run", action="store_true")
    return parser
