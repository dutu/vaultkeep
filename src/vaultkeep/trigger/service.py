"""Explicit management of one systemd user monitor service per job."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

from vaultkeep.config.identity import JobIdentity
from vaultkeep.installation import installation_path, systemd_argument
from vaultkeep.maintenance import command_selection, require_generated
from vaultkeep.trigger.config import TriggerConfig
from vaultkeep.trigger.errors import BlockedError, ConfigurationError
from vaultkeep.trigger.paths import account_home, validate_scope


def service_name(config: TriggerConfig | JobIdentity) -> str:
    assert config.scope is not None
    return f"vaultkeep-trigger@{config.job_id}-{validate_scope(config.scope)}.service"


def units_root() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(value) if value and Path(value).is_absolute() else account_home() / ".config"
    return base / "systemd/user"


def _systemctl(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "--user", *arguments],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BlockedError(f"cannot contact the systemd user manager: {error}") from error
    if result.returncode:
        raise BlockedError(
            f"systemctl --user {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout


def _quote_systemd_argument(value: str, *, executable: bool = False) -> str:
    """Quote one ExecStart argument with systemd syntax and literal expansions."""
    try:
        return systemd_argument(value, executable=executable)
    except ValueError as error:
        raise ConfigurationError(str(error)) from error


def render_service(config: TriggerConfig | JobIdentity) -> str:
    # Always use the stable installed entry point, matching timer services.
    args = [
        str(installation_path("VAULTKEEP_BIN_LINK")),
        "--user",
        "--config",
        str(config.source_path),
    ]
    for key, value in sorted(config.params.items()):
        args.extend(["--param", f"{key}={value}"])
    args.extend(["trigger", "monitor"])
    # ExecStart is parsed by systemd, not by a shell.
    command = " ".join(
        _quote_systemd_argument(arg, executable=index == 0) for index, arg in enumerate(args)
    )
    return (
        f"# Managed by Vaultkeep: {service_name(config)}\n"
        "[Unit]\n"
        f"Description=Vaultkeep shutdown monitor for {config.job_id}\n\n"
        "[Service]\nType=simple\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\nRestartSec=5\nUMask=0077\n"
        "StandardOutput=journal\nStandardError=journal\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def inspect_service(config: TriggerConfig | JobIdentity) -> dict[str, str]:
    """Verify the on-disk and effective unit selection before trusting its state."""
    from vaultkeep.errors import TimerError
    from vaultkeep.schedule.status import unit_properties

    unit = service_name(config)
    destination = units_root() / unit
    try:
        values = unit_properties(unit, user_mode=True)
        if (
            values["LoadState"] == "not-found"
            and not destination.exists()
            and not destination.is_symlink()
        ):
            return values
        info = destination.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("monitor unit must be a regular file owned by the current user")
        content = destination.read_text(encoding="utf-8")
        if not content.startswith(f"# Managed by Vaultkeep: {unit}\n"):
            raise ValueError("unmanaged monitor service")
        if destination.with_name(unit + ".d").exists():
            raise ValueError("monitor service has local overrides; review them first")
        source, params = command_selection(content, monitor=True, user=True)
        if source.resolve() != config.source_path or params != config.params:
            raise ValueError("monitor command conflicts with the requested job identity")
        require_generated(content, render_service(config), variable=("ExecStart",))
        if values.get("FragmentPath") != str(destination) or values.get("DropInPaths") != "":
            raise ValueError("effective monitor unit ownership is unverified; review systemctl cat")
        if values.get("NeedDaemonReload") != "no":
            raise ValueError(
                "monitor unit changed or cannot be verified; review and daemon-reload first"
            )
        return values
    except (OSError, UnicodeError, ValueError, TimerError) as error:
        raise ConfigurationError(f"Cannot verify managed monitor {unit}: {error}") from error


def _require_safe_stop(config: TriggerConfig | JobIdentity) -> None:
    from vaultkeep.trigger.paths import state_root
    from vaultkeep.trigger.state import StateStore

    store = StateStore(state_root(config.scope))
    state = store.read_instance(config.job_id)
    blocked = store.read_blocked(config.job_id)
    _, attempt = store.dispatch_activity(config.job_id, state, blocked)
    if (
        blocked
        or attempt
        or (state and (state.active_review_required or state.blocking_attempt_id))
    ):
        raise BlockedError(
            "Cannot stop monitor with active or unreconciled backup/safety state; "
            "inspect status and review Trigger recovery records before retrying"
        )


def manage_service(config: TriggerConfig | JobIdentity, action: str) -> None:
    unit = service_name(config)
    destination = units_root() / unit
    if action in ("disable", "remove"):
        from vaultkeep.backup.paths import user_workflow_paths
        from vaultkeep.errors import LockError
        from vaultkeep.locking import JobLock, job_lock_path

        values = inspect_service(config)
        if values["LoadState"] == "not-found":
            raise ConfigurationError(
                f"managed monitor service not found: {unit}; "
                "use the original configuration path, job.id, and parameters"
            )
        assert config.scope is not None
        lock = JobLock(
            job_lock_path(
                root=user_workflow_paths().lock_root,
                job_id=config.job_id,
                identity_hash=config.scope,
            )
        )
        try:
            lock.acquire()
            _require_safe_stop(config)
            _systemctl("disable", "--now", unit)
        except LockError as error:
            raise BlockedError(
                f"Cannot stop monitor: {error}; wait for the backup to finish"
            ) from error
        finally:
            lock.release()
        if action == "remove":
            destination.unlink()
            _systemctl("daemon-reload")
        return
    marker = f"# Managed by Vaultkeep: {unit}\n"
    if destination.is_symlink() or (
        destination.exists() and not destination.read_text().startswith(marker)
    ):
        raise ConfigurationError(f"refusing to replace an unmanaged monitor service: {destination}")
    if destination.with_name(unit + ".d").exists():
        raise ConfigurationError(f"monitor service has local overrides; review them first: {unit}")
    assert isinstance(config, TriggerConfig)
    if not config.enabled_instances():
        raise ConfigurationError("monitor installation requires trigger.enabled: true")
    if action == "update" and not destination.exists():
        raise ConfigurationError(f"managed monitor service not found; use trigger install: {unit}")
    content = render_service(config)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".vaultkeep-trigger-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", unit)
    if action == "update":
        _systemctl("restart", unit)
