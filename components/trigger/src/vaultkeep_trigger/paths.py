"""Filesystem and environment path resolution.

Trigger's own state root is fixed at ``<account-home>/.local/state/vaultkeep-trigger``.
It is resolved from the operating-system account record for the effective UID and is
deliberately independent of ``HOME`` and ``XDG_STATE_HOME`` so that the monitor, the
CLI, and workers agree on it.

VaultKeep's child environment, by contrast, honors XDG paths and shares the user's own
runtime directory so its job lock stays shared with manual and timer-driven runs.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

from vaultkeep_trigger.errors import BlockedError, ConfigurationError

STATE_DIR_NAME = "vaultkeep-trigger"
RUNTIME_SUBDIR_NAME = "vaultkeep-trigger"
CONFIG_DIR_NAME = "vaultkeep-trigger"
CONFIG_FILE_NAME = "config.toml"
MONITOR_LOCK_NAME = "monitor.lock"


def account_home() -> Path:
    """Return the account home directory from the OS account record for the euid."""
    entry = pwd.getpwuid(os.geteuid())
    home = entry.pw_dir
    if not home:
        raise BlockedError(f"account record for uid {os.geteuid()} has no home directory")
    return Path(home)


def state_root() -> Path:
    """Return Trigger's fixed state root, independent of HOME/XDG overrides."""
    return account_home() / ".local" / "state" / STATE_DIR_NAME


def config_path(override: str | os.PathLike[str] | None = None) -> Path:
    """Return the configuration path, honoring an explicit override."""
    if override is not None:
        return Path(override)
    configured = os.environ.get("XDG_CONFIG_HOME")
    if configured and os.path.isabs(configured):
        base = Path(configured)
    else:
        base = account_home() / ".config"
    return base / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def _validated_dir(value: str | None, *, uid: int) -> Path | None:
    if not value or not os.path.isabs(value):
        return None
    path = Path(value)
    try:
        stat_result = path.stat()
    except OSError:
        return None
    if not path.is_dir():
        return None
    if stat_result.st_uid != uid:
        return None
    return path


def runtime_dir(*, required: bool = False) -> Path | None:
    """Return the user's runtime directory, or ``None`` when unusable.

    Fails closed when ``required`` is set because ``XDG_RUNTIME_DIR`` is needed for
    ``monitor.lock`` and for VaultKeep's preferred lock namespace.
    """
    uid = os.geteuid()
    resolved = _validated_dir(os.environ.get("XDG_RUNTIME_DIR"), uid=uid)
    if resolved is None:
        if required:
            raise BlockedError(
                "XDG_RUNTIME_DIR is unset, not an absolute directory, or not owned by the "
                "current user; a runtime directory is required"
            )
        return None
    return resolved


def trigger_runtime_dir(*, required: bool = False) -> Path | None:
    """Return Trigger's runtime subdirectory holding ``monitor.lock``."""
    base = runtime_dir(required=required)
    if base is None:
        return None
    return base / RUNTIME_SUBDIR_NAME


def monitor_lock_path() -> Path:
    """Return the monitor lock path, failing closed when no runtime dir exists."""
    subdir = trigger_runtime_dir(required=True)
    assert subdir is not None
    return subdir / MONITOR_LOCK_NAME


def expand_user_path(value: str) -> str:
    """Expand a leading ``~`` using the account home, not ``$HOME``."""
    if value == "~":
        return str(account_home())
    if value.startswith("~/"):
        return str(account_home() / value[2:])
    return value


def resolve_absolute_path(value: str, *, what: str) -> Path:
    """Expand ``~`` and require an absolute, non-empty path."""
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{what} must be a non-empty string")
    expanded = expand_user_path(value)
    if not os.path.isabs(expanded):
        raise ConfigurationError(f"{what} must be an absolute path after home expansion")
    return Path(expanded)
