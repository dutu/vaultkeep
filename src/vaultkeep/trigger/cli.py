"""Command-line interface for monitoring, status, services, and workers."""

from __future__ import annotations

import argparse
import logging
import os
import signal
from pathlib import Path
from types import FrameType

from vaultkeep.application import PsutilProcessProvider
from vaultkeep.cli.output import Console, configure_logging
from vaultkeep.config import parse_runtime_parameters
from vaultkeep.errors import ConfigurationError as JobConfigurationError
from vaultkeep.notifications.notifier import build_notifier
from vaultkeep.trigger.config import (
    TriggerConfig,
    load_config,
    validate_vaultkeep_executable,
)
from vaultkeep.trigger.errors import (
    EXIT_CONFIG,
    EXIT_OK,
    SelectionError,
    TriggerError,
)
from vaultkeep.trigger.executor import exec_worker
from vaultkeep.trigger.monitor import Monitor
from vaultkeep.trigger.paths import (
    state_root,
    validate_scope,
)
from vaultkeep.trigger.preflight import runtime_preflight
from vaultkeep.trigger.service import manage_service, service_name
from vaultkeep.trigger.state import StateStore
from vaultkeep.trigger.systemd import RealSystemdController

_LOG = logging.getLogger("vaultkeep.trigger")


def _load(args: argparse.Namespace) -> TriggerConfig:
    try:
        params = parse_runtime_parameters(args.param)
    except JobConfigurationError as error:
        raise SelectionError(str(error)) from error
    config = load_config(args.config, runtime_params=params)
    configure_logging(config.log_level, no_color=args.no_color)
    return config


# -- monitor ------------------------------------------------------------------
def cmd_monitor(args: argparse.Namespace) -> int:
    config = _load(args)
    from vaultkeep.trigger.service import require_monitor_identity

    validate_vaultkeep_executable(config)
    if not config.enabled_instances():
        raise SelectionError("monitoring requires trigger.enabled: true")
    require_monitor_identity(config, args.expected_identity)
    store = StateStore(state_root(config.scope))
    monitor = Monitor(
        config,
        store=store,
        provider=PsutilProcessProvider(owner_uid=config.enabled_instances()[0].owner_uid),
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
    command = args.action
    if not args.user_mode:
        parser.error("Trigger supports user mode only; specify --user before trigger")
    if os.geteuid() == 0:
        Console(no_color=args.no_color).issue(
            "Trigger cannot run as root; run it without sudo as the application owner."
        )
        return EXIT_CONFIG
    if command != "exec-worker" and args.config is None:
        parser.error("trigger commands require --config pointing to a job YAML")
    try:
        if command == "monitor":
            return cmd_monitor(args)
        if command == "exec-worker":
            return cmd_exec_worker(args)
        config: TriggerConfig | Path
        if command in ("disable", "remove"):
            config = Path(args.config)
        else:
            full_config = _load(args)
            config = full_config
            if command in ("install", "update"):
                validate_vaultkeep_executable(full_config)
                runtime_preflight(
                    full_config,
                    RealSystemdController(),
                    state_root(full_config.scope),
                    warning=lambda message: Console(no_color=args.no_color).issue(
                        message, warning=True
                    ),
                )
        manage_service(config, command)
        _LOG.info("Monitor service unit: %s", service_name(config))
        Console(no_color=args.no_color).fields(
            [
                (f"Trigger {command}", "OK"),
            ]
        )
        return EXIT_OK
    except TriggerError as error:
        Console(no_color=args.no_color).issue(f"{type(error).__name__}: {error}")
        return error.exit_code
    except JobConfigurationError as error:
        Console(no_color=args.no_color).issue(f"ConfigurationError: {error}")
        return EXIT_CONFIG
    except OSError as error:
        Console(no_color=args.no_color).issue(f"Trigger filesystem error: {error}")
        return EXIT_CONFIG
