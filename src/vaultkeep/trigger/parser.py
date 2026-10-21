"""Command-line arguments for the optional shutdown trigger."""

from __future__ import annotations

import argparse
from pathlib import Path


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.description = (
        "Shutdown triggering is user-mode only. Specify --user before trigger and run without sudo."
    )
    parser.add_argument("--version", action="store_true", dest="trigger_version")
    parser.add_argument("--config", type=Path, dest="trigger_config", help="path to the job YAML")
    parser.add_argument(
        "--param", action="append", default=[], dest="trigger_params", metavar="KEY=VALUE"
    )
    commands = parser.add_subparsers(dest="trigger_command")
    commands.add_parser("monitor", help="monitor this job in the foreground")
    check = commands.add_parser("check-config", help="validate the job and trigger")
    check.add_argument("--runtime", action="store_true")
    status = commands.add_parser("status", help="report this job's persisted trigger state")
    status.add_argument("instance_id", nargs="?", help=argparse.SUPPRESS)
    status.add_argument("--json", action="store_true", help="report Trigger activity as JSON")
    for action, help_text in (
        ("install", "install and enable this job's user monitor service"),
        ("update", "refresh and restart this job's user monitor service"),
        ("disable", "stop and disable this job's monitor; workers keep running"),
        ("remove", "remove this job's monitor service; preserve recovery state"),
    ):
        commands.add_parser(action, help=help_text)
    worker = commands.add_parser("exec-worker", help="internal worker service entry point")
    worker.add_argument("attempt_id")
