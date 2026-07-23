"""Stable local identity and state-path derivation."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

DEFAULT_STATE_ROOT = Path("/var/lib/vaultkeep/jobs")


def canonical_config_path(config_path: Path) -> Path:
    """Return the canonical absolute configuration path."""
    return Path(os.path.realpath(os.path.abspath(config_path)))


def job_identity_hash(config_path: Path, job_id: str) -> str:
    """Calculate the documented 16-hex local job identity."""
    digest = hashlib.sha256()
    digest.update(os.fsencode(canonical_config_path(config_path)))
    digest.update(b"\0")
    digest.update(job_id.encode("ascii"))
    return digest.hexdigest()[:16]


def job_state_path(
    config_path: Path,
    job_id: str,
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> Path:
    """Return the state.json path for one configuration identity."""
    identity = job_identity_hash(config_path, job_id)
    return state_root / f"{job_id}-{identity}" / "state.json"
