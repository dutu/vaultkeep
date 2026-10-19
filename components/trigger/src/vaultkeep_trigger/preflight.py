"""Read-only configuration and runtime checks shared by the CLI and monitor."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from vaultkeep_trigger.childproc import ChildRunner, RealChildRunner
from vaultkeep_trigger.config import TriggerConfig, validate_vaultkeep_executable
from vaultkeep_trigger.environment import build_child_environment
from vaultkeep_trigger.errors import BlockedError, ConfigurationError, IntegrationError
from vaultkeep_trigger.paths import runtime_dir, trigger_runtime_dir
from vaultkeep_trigger.systemd import SystemdController
from vaultkeep_trigger.version import DEFAULT_PREFLIGHT_TIMEOUT_SECONDS, REVIEWED_VAULTKEEP_VERSION

_LOG = logging.getLogger(__name__)


def validate_jobs(
    config: TriggerConfig, *, runtime: bool, runner: ChildRunner | None = None
) -> None:
    runner = runner or RealChildRunner()
    env = build_child_environment()
    for instance in config.enabled_instances():
        argv = [str(config.vaultkeep), *instance.job.argv(), "validate"]
        if not runtime:
            argv.append("--schema-only")
        result = runner.run(argv, env=env, timeout=DEFAULT_PREFLIGHT_TIMEOUT_SECONDS)
        if result.spawn_error or result.exit_code == 2:
            raise IntegrationError(
                f"{instance.id}: VaultKeep validation integration error "
                f"(exit {result.exit_code}): {result.spawn_error or 'invalid arguments'}"
            )
        if result.exit_code != 0:
            diagnostics = result.diagnostics.decode("utf-8", "replace").strip()
            raise ConfigurationError(
                f"{instance.id}: job validation failed "
                f"(VaultKeep exit {result.exit_code}, signal {result.signal_number})"
                + (f"\n{diagnostics}" if diagnostics else "")
            )


def _check_writable_location(path: Path) -> None:
    """Inspect the existing parent chain without creating any probe files."""
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not existing.is_dir() or not os.access(existing, os.W_OK | os.X_OK):
        raise BlockedError(f"location is not writable through its existing parent: {path}")


def runtime_preflight(
    config: TriggerConfig,
    controller: SystemdController,
    state_path: Path,
    *,
    runner: ChildRunner | None = None,
) -> str | None:
    """Check readiness and return the installed version without mutating state/services."""
    validate_vaultkeep_executable(config)
    if not controller.manager_reachable():
        raise BlockedError("the systemd user manager is not reachable")
    if not controller.template_ready():
        raise BlockedError(
            "worker template is absent, masked, invalid, or its executable is unavailable"
        )
    runtime = runtime_dir(required=True)
    assert runtime is not None
    _check_writable_location(runtime)
    subdir = trigger_runtime_dir(required=True)
    assert subdir is not None
    _check_writable_location(subdir)
    for path in (
        state_path,
        state_path / "instances",
        state_path / "attempts",
        state_path / "blocked",
    ):
        _check_writable_location(path)
    runner = runner or RealChildRunner()
    validate_jobs(config, runtime=True, runner=runner)
    result = runner.run(
        [str(config.vaultkeep), "--version"],
        env=build_child_environment(),
        timeout=DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
    )
    if result.spawn_error:
        raise IntegrationError("failed to execute VaultKeep version preflight")
    version = result.stdout_tail.strip() if result.exit_code == 0 and result.stdout_tail else None
    if version != REVIEWED_VAULTKEEP_VERSION:
        _LOG.warning(
            "installed VaultKeep version differs from reviewed contract %s",
            REVIEWED_VAULTKEEP_VERSION,
        )
    return version
