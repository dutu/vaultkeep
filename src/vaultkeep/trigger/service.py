"""Explicit management of one systemd user monitor service per job."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from vaultkeep.trigger.config import TriggerConfig
from vaultkeep.trigger.errors import BlockedError, ConfigurationError
from vaultkeep.trigger.paths import account_home, validate_scope


def service_name(config: TriggerConfig) -> str:
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


def render_service(config: TriggerConfig) -> str:
    # Always use the stable installed entry point, matching timer services.
    args = ["/usr/local/bin/vaultkeep", "--user", "trigger", "--config", str(config.source_path)]
    for key, value in sorted(config.params.items()):
        args.extend(["--param", f"{key}={value}"])
    args.append("monitor")
    if any(ord(char) < 32 or ord(char) == 127 for arg in args for char in arg):
        raise ConfigurationError("monitor command arguments must not contain control characters")
    # systemd expands specifiers and environment variables even inside quoted argv.
    command = shlex.join(args).replace("%", "%%").replace("$", "$$")
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


def manage_service(config: TriggerConfig, action: str) -> None:
    unit = service_name(config)
    destination = units_root() / unit
    marker = f"# Managed by Vaultkeep: {unit}\n"
    if destination.is_symlink() or (
        destination.exists() and not destination.read_text().startswith(marker)
    ):
        raise ConfigurationError(f"refusing to replace an unmanaged monitor service: {destination}")
    if destination.with_name(unit + ".d").exists():
        raise ConfigurationError(f"monitor service has local overrides; review them first: {unit}")
    if action in ("disable", "remove"):
        if not destination.exists():
            raise ConfigurationError(f"managed monitor service not found: {unit}")
        _systemctl("disable", "--now", unit)
        if action == "remove":
            destination.unlink()
            _systemctl("daemon-reload")
        return
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
