"""Shared root and user execution paths, without filesystem side effects."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkflowPaths:
    """Explicit testable locations for local state and private archive workspaces."""

    state_root: Path = Path("/var/lib/vaultkeep/jobs")
    local_temp_root: Path = Path("/var/lib/vaultkeep/tmp")
    lock_root: Path = Path("/run/lock/vaultkeep")


def user_workflow_paths(*, environ: Mapping[str, str] | None = None) -> WorkflowPaths:
    """Return per-user state, temp, and lock paths for non-root executions."""
    environ = os.environ if environ is None else environ
    account_home = Path(environ.get("HOME", str(Path.home())))
    state_base = Path(environ.get("XDG_STATE_HOME", account_home / ".local" / "state"))
    cache_base = Path(environ.get("XDG_CACHE_HOME", account_home / ".cache"))
    runtime_base = environ.get("XDG_RUNTIME_DIR")
    lock_base = Path(runtime_base) if runtime_base else cache_base
    return WorkflowPaths(
        state_root=state_base / "vaultkeep" / "jobs",
        local_temp_root=cache_base / "vaultkeep" / "tmp",
        lock_root=lock_base / "vaultkeep" / "locks",
    )
