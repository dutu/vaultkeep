"""Command-line arguments for the optional shutdown trigger."""

from __future__ import annotations

import argparse


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.description = (
        "Shutdown triggering is user-mode only. Put --user, --config and --param before trigger "
        "and run without sudo."
    )
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("monitor", help="monitor this job in the foreground")
    for action, help_text in (
        ("install", "install and enable this job's user monitor service"),
        ("update", "refresh and restart this job's user monitor service"),
        ("disable", "stop and disable this job's monitor; workers keep running"),
        ("remove", "remove this job's monitor service; preserve recovery state"),
    ):
        commands.add_parser(action, help=help_text)
    worker = commands.add_parser("exec-worker", help="internal worker service entry point")
    worker.add_argument("attempt_id")
