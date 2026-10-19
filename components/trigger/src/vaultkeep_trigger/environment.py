"""Explicit child environment for the VaultKeep subprocess."""

from __future__ import annotations

import os
from pathlib import Path

from vaultkeep_trigger.paths import account_home

#: Fallback PATH used when the monitor's own PATH is not a safe absolute list.
_SAFE_PATH_FALLBACK = "/usr/local/bin:/usr/bin:/bin"


def _validated_absolute_dir(value: str | None) -> str | None:
    if not value or not os.path.isabs(value):
        return None
    return value


def build_child_environment(
    *,
    home: Path | None = None,
    runtime_dir: Path | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a validated environment mapping for the VaultKeep child process.

    ``XDG_RUNTIME_DIR`` is passed unmodified so VaultKeep's ``vaultkeep/locks``
    namespace matches manual and timer-driven runs. Trigger's own runtime subdirectory
    is never used here. Arbitrary worker-unit variables are not inherited.
    """
    source = os.environ if environ is None else environ
    account = home if home is not None else account_home()

    resolved_runtime = runtime_dir
    if resolved_runtime is None:
        candidate = _validated_absolute_dir(source.get("XDG_RUNTIME_DIR"))
        resolved_runtime = Path(candidate) if candidate else None

    state_home = _validated_absolute_dir(source.get("XDG_STATE_HOME")) or str(
        account / ".local" / "state"
    )
    cache_home = _validated_absolute_dir(source.get("XDG_CACHE_HOME")) or str(account / ".cache")

    env: dict[str, str] = {
        "HOME": str(account),
        "XDG_STATE_HOME": state_home,
        "XDG_CACHE_HOME": cache_home,
        "PATH": _SAFE_PATH_FALLBACK,
    }
    if resolved_runtime is not None:
        env["XDG_RUNTIME_DIR"] = str(resolved_runtime)

    lang = source.get("LANG")
    if lang:
        env["LANG"] = lang
    return env
