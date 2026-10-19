"""Command-line interface: monitor, check-config, status, and exec-worker."""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import sys

from vaultkeep_trigger.config import (
    TriggerConfig,
    load_config,
    validate_vaultkeep_executable,
)
from vaultkeep_trigger.errors import (
    EXIT_CONFIG,
    EXIT_OK,
    BlockedError,
    SelectionError,
    StateError,
    TriggerError,
)
from vaultkeep_trigger.executor import exec_worker
from vaultkeep_trigger.models import InstanceState, parse_utc, utc_now
from vaultkeep_trigger.monitor import Monitor
from vaultkeep_trigger.notify import build_notifier
from vaultkeep_trigger.paths import (
    config_path,
    monitor_lock_path,
    state_root,
)
from vaultkeep_trigger.preflight import runtime_preflight, validate_jobs
from vaultkeep_trigger.process import PsutilProcessProvider
from vaultkeep_trigger.state import StateStore
from vaultkeep_trigger.systemd import RealSystemdController
from vaultkeep_trigger.version import TRIGGER_VERSION

_LOG = logging.getLogger("vaultkeep_trigger")


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vaultkeep-trigger")
    parser.add_argument("--version", action="store_true", dest="show_version")
    parser.add_argument("--config", dest="config_override", default=None)
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("monitor", help="run the foreground monitor")

    check = subparsers.add_parser("check-config", help="validate configuration")
    check.add_argument("--runtime", action="store_true")

    status = subparsers.add_parser("status", help="report persisted state (read-only)")
    status.add_argument("instance_id", nargs="?")

    worker = subparsers.add_parser("exec-worker", help=argparse.SUPPRESS)
    worker.add_argument("attempt_id")
    return parser


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _load(args: argparse.Namespace) -> TriggerConfig:
    path = config_path(args.config_override)
    config = load_config(path)
    _configure_logging(config.log_level)
    return config


# -- check-config -------------------------------------------------------------
def _validate_instance_jobs(config: TriggerConfig, *, runtime: bool) -> int:
    validate_jobs(config, runtime=runtime)
    return EXIT_OK


def cmd_check_config(args: argparse.Namespace) -> int:
    config = _load(args)
    validate_vaultkeep_executable(config)
    print(f"configuration: {config.source_path}")
    print(f"vaultkeep: {config.vaultkeep}")
    print(f"instances: {len(config.instances)} ({len(config.enabled_instances())} enabled)")

    if args.runtime:
        controller = RealSystemdController()
        root = state_root()
        version = runtime_preflight(config, controller, root)
        print(f"state root: {root}")
        print(f"vaultkeep version: {version or 'unknown'}")
        return EXIT_OK

    return _validate_instance_jobs(config, runtime=args.runtime)


# -- monitor ------------------------------------------------------------------
def cmd_monitor(args: argparse.Namespace) -> int:
    config = _load(args)
    validate_vaultkeep_executable(config)
    store = StateStore(state_root())
    monitor = Monitor(
        config,
        store=store,
        provider=PsutilProcessProvider(),
        controller=RealSystemdController(),
        notifier=build_notifier(config.notifications),
    )

    def _stop(signum, _frame):
        _LOG.info("received signal %s; stopping monitoring", signum)
        monitor.request_stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    monitor.run()
    return EXIT_OK


# -- status -------------------------------------------------------------------
def _monitor_runtime_status() -> str:
    try:
        path = monitor_lock_path()
    except BlockedError:
        return "unavailable (no runtime directory)"
    if not path.exists():
        return "not running (no lock file)"
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return "unknown (lock file not readable)"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            owner = path.read_text(encoding="utf-8").strip() or "unknown"
            return f"running (lock held by pid {owner})"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "not running (lock is free)"
    finally:
        os.close(fd)


def _format_age(timestamp: str | None) -> str:
    parsed = parse_utc(timestamp)
    if parsed is None:
        return "unknown"
    now = parse_utc(utc_now())
    if now is None:
        return "unknown"
    return f"{(now - parsed).total_seconds():.0f}s ago"


def _print_instance(state: InstanceState, blocked_reason: str | None = None) -> None:
    print(f"instance {state.instance_id}")
    if blocked_reason is not None:
        print(f"  dispatch blocked: {blocked_reason}")
    print(f"  config fingerprint: {state.config_fingerprint or 'unknown'}")
    print(f"  armed: {state.armed}")
    if state.last_observation is not None:
        obs = state.last_observation
        detail = f" ({obs.reason})" if obs.reason else ""
        print(f"  observation: {obs.observation.value}{detail} [{_format_age(obs.observed_at)}]")
    else:
        print("  observation: none recorded")
    if state.pending_request is not None:
        print(f"  pending request: {state.pending_request.request_id}")
    if state.active_attempt_id is not None:
        print(f"  active attempt: {state.active_attempt_id}")
    if state.pending_request is not None and state.active_attempt_id is not None:
        print(
            f"  blocked behind attempt {state.blocking_attempt_id} "
            f"(waiting {_format_age(state.blocked_wait_started_at)})"
        )
    if state.rejection is not None and state.rejection.consecutive:
        print(f"  consecutive submission rejections: {state.rejection.consecutive}")
    if state.review_required:
        print(f"  review required: {state.review_reason}")
    if state.attempt_summaries:
        latest = state.attempt_summaries[-1]
        print(f"  last attempt outcome: {latest.get('outcome')}")
    print(f"  last monitor update: {_format_age(state.last_monitor_update)}")


def cmd_status(args: argparse.Namespace) -> int:
    store = StateStore(state_root())
    runtime = _monitor_runtime_status()
    print(f"monitor: {runtime}")
    instance_ids = sorted(set(store.list_instance_ids()) | set(store.list_blocked_ids()))
    if not instance_ids:
        print("state: no recorded instances")
        return EXIT_OK
    if args.instance_id is not None and args.instance_id not in instance_ids:
        raise SelectionError(f"unknown instance: {args.instance_id}")
    exit_code = EXIT_OK
    for instance_id in instance_ids:
        if args.instance_id is not None and instance_id != args.instance_id:
            continue
        try:
            blocked_reason = store.read_blocked(instance_id)
        except StateError as error:
            blocked_reason = str(error)
            print(f"StateError: {error}", file=sys.stderr)
            exit_code = EXIT_CONFIG
        try:
            state = store.read_instance(instance_id)
        except StateError as error:
            print(f"StateError: {error}", file=sys.stderr)
            print(f"instance {instance_id}\n  state unavailable")
            if blocked_reason is not None:
                print(f"  dispatch blocked: {blocked_reason}")
            exit_code = EXIT_CONFIG
            continue
        if state is None:
            if blocked_reason is not None:
                print(f"instance {instance_id}\n  dispatch blocked: {blocked_reason}")
            continue
        _print_instance(state, blocked_reason)
    return exit_code


# -- exec-worker --------------------------------------------------------------
def cmd_exec_worker(args: argparse.Namespace) -> int:
    store = StateStore(state_root())
    result = exec_worker(args.attempt_id, store=store)
    print(result.message)
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.show_version:
        print(TRIGGER_VERSION)
        return EXIT_OK
    if args.command is None:
        parser.error("a command is required")

    try:
        if args.command == "monitor":
            return cmd_monitor(args)
        if args.command == "check-config":
            return cmd_check_config(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "exec-worker":
            return cmd_exec_worker(args)
        parser.error(f"unknown command {args.command}")
    except TriggerError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return error.exit_code
