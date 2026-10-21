"""Command-line interface: monitor, check-config, status, and exec-worker."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import sys
from types import FrameType

from vaultkeep.config import parse_runtime_parameters
from vaultkeep.errors import ConfigurationError as JobConfigurationError
from vaultkeep.trigger.config import (
    TriggerConfig,
    load_config,
    validate_vaultkeep_executable,
)
from vaultkeep.trigger.errors import (
    EXIT_CONFIG,
    EXIT_OK,
    BlockedError,
    SelectionError,
    StateError,
    TriggerError,
)
from vaultkeep.trigger.executor import exec_worker
from vaultkeep.trigger.models import InstanceState, parse_utc, utc_now
from vaultkeep.trigger.monitor import Monitor
from vaultkeep.trigger.notify import build_notifier
from vaultkeep.trigger.paths import (
    monitor_lock_path,
    state_root,
    validate_scope,
)
from vaultkeep.trigger.preflight import runtime_preflight, validate_jobs
from vaultkeep.trigger.process import PsutilProcessProvider
from vaultkeep.trigger.service import manage_service, service_name
from vaultkeep.trigger.state import StateStore
from vaultkeep.trigger.systemd import RealSystemdController
from vaultkeep.version import installed_version

_LOG = logging.getLogger("vaultkeep.trigger")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _load(args: argparse.Namespace) -> TriggerConfig:
    try:
        params = parse_runtime_parameters([*args.param, *args.trigger_params])
    except JobConfigurationError as error:
        raise SelectionError(str(error)) from error
    config = load_config(args.trigger_config or args.config, runtime_params=params)
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
        root = state_root(config.scope)
        version = runtime_preflight(config, controller, root)
        print(f"state root: {root}")
        print(f"vaultkeep version: {version or 'unknown'}")
        return EXIT_OK

    return _validate_instance_jobs(config, runtime=args.runtime)


# -- monitor ------------------------------------------------------------------
def cmd_monitor(args: argparse.Namespace) -> int:
    config = _load(args)
    validate_vaultkeep_executable(config)
    if not config.enabled_instances():
        raise SelectionError("monitoring requires trigger.enabled: true")
    store = StateStore(state_root(config.scope))
    monitor = Monitor(
        config,
        store=store,
        provider=PsutilProcessProvider(),
        controller=RealSystemdController(),
        notifier=build_notifier(config.notifications),
    )

    def _stop(signum: int, _frame: FrameType | None) -> None:
        _LOG.info("received signal %s; stopping monitoring", signum)
        monitor.request_stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    monitor.run()
    return EXIT_OK


# -- status -------------------------------------------------------------------
def _monitor_status(scope: str | None) -> str:
    """Probe the monitor's exclusive lifetime lock without taking an exclusive lock."""
    try:
        path = monitor_lock_path(scope)
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return "stopped"
    except (BlockedError, OSError):
        return "unknown"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "running"
        except OSError:
            return "unknown"
        return "stopped"
    finally:
        os.close(fd)


def _monitor_runtime_status(scope: str | None = None) -> str:
    try:
        path = monitor_lock_path(scope)
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


def _cmd_status_json(args: argparse.Namespace, config: TriggerConfig, store: StateStore) -> int:
    instance_id = args.instance_id or config.job_id
    activity = "unknown"
    pending_request_id = None
    active_attempt_id = None
    exit_code = EXIT_OK
    try:
        if args.instance_id is not None and args.instance_id not in (
            set(store.list_instance_ids()) | set(store.list_blocked_ids())
        ):
            raise SelectionError(f"unknown instance: {instance_id}")
        state = store.read_instance(instance_id)
        blocked = store.read_blocked(instance_id)
        if args.instance_id is not None and state is None and blocked is None:
            raise SelectionError(f"unknown instance: {instance_id}")
        # Missing state is not evidence of idleness. An unassociated launch can
        # still supply positive evidence of an unresolved attempt.
        recovery_state = state or InstanceState(instance_id=instance_id)
        pending = recovery_state.pending_request
        pending_request_id = pending.request_id if pending is not None else None
        active_attempt_id = recovery_state.active_attempt_id
        for attempt_id in store.list_attempt_ids():
            request = store.read_request(attempt_id)
            if request is None:
                if store.read_launch(attempt_id) or store.read_receipt(attempt_id):
                    raise StateError(f"attempt {attempt_id} has no request")
                continue
            if request.instance_id != instance_id:
                continue
            if request.attempt_id != attempt_id:
                raise StateError(f"attempt {attempt_id} has contradictory request ownership")
            if store.attempt_is_protected(recovery_state, attempt_id, request):
                if active_attempt_id is not None and active_attempt_id != attempt_id:
                    raise StateError(f"instance {instance_id} has multiple unreconciled attempts")
                active_attempt_id = attempt_id
        if active_attempt_id is not None:
            activity = "active"
        elif state is not None and blocked is None:
            activity = "pending" if pending_request_id is not None else "idle"
    except (StateError, OSError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        exit_code = EXIT_CONFIG
    print(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": config.job_id,
                "backup_activity": activity,
                "pending_request_id": pending_request_id,
                "active_attempt_id": active_attempt_id,
                "monitor_status": _monitor_status(config.scope),
            },
            sort_keys=True,
        )
    )
    return exit_code


def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args)
    store = StateStore(state_root(config.scope))
    if args.json:
        return _cmd_status_json(args, config, store)
    runtime = _monitor_runtime_status(config.scope)
    print(f"service: {service_name(config)}")
    print(f"state root: {store.root}")
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
    import uuid

    scope, separator, attempt_id = args.attempt_id.partition("_")
    if not separator:
        raise SelectionError("worker requires a job namespace and attempt ID")
    validate_scope(scope)
    try:
        parsed = uuid.UUID(attempt_id)
    except ValueError as error:
        raise SelectionError("invalid worker attempt ID") from error
    if str(parsed) != attempt_id or parsed.version != 4:
        raise SelectionError("invalid worker attempt ID")
    store = StateStore(state_root(scope))
    result = exec_worker(attempt_id, store=store)
    print(result.message)
    return result.exit_code


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.trigger_version:
        print(installed_version())
        return EXIT_OK
    command = args.trigger_command
    if command is None:
        parser.error("a trigger command is required")
    if not args.user_mode:
        parser.error("Trigger supports user mode only; specify --user before trigger")
    if os.geteuid() == 0:
        print(
            "Trigger cannot run as root; run it without sudo as the application owner.",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    if command != "exec-worker" and args.trigger_config is None and args.config is None:
        parser.error("trigger commands require --config pointing to a job YAML")
    if args.trigger_config is not None and args.config is not None:
        parser.error("specify --config once, before or after trigger")
    try:
        if command == "monitor":
            return cmd_monitor(args)
        if command == "check-config":
            return cmd_check_config(args)
        if command == "status":
            return cmd_status(args)
        if command == "exec-worker":
            return cmd_exec_worker(args)
        config = _load(args)
        if command in ("install", "update"):
            validate_vaultkeep_executable(config)
            runtime_preflight(config, RealSystemdController(), state_root(config.scope))
        manage_service(config, command)
        print(f"{command}: {service_name(config)}")
        return EXIT_OK
    except TriggerError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return error.exit_code
    except OSError as error:
        print(f"Trigger filesystem error: {error}", file=sys.stderr)
        return EXIT_CONFIG
