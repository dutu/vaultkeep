"""Stable local identity and state-path derivation."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

DEFAULT_STATE_ROOT = Path("/var/lib/vaultkeep/jobs")


def canonical_config_path(config_path: Path) -> Path:
    """Return the canonical absolute configuration path."""
    return Path(os.path.realpath(os.path.abspath(config_path)))


def job_identity_hash(
    config_path: Path, job_id: str, *, runtime_params: Mapping[str, str] | None = None
) -> str:
    """Calculate the documented 16-hex local job identity."""
    digest = hashlib.sha256()
    digest.update(os.fsencode(canonical_config_path(config_path)))
    digest.update(b"\0")
    digest.update(job_id.encode("ascii"))
    if runtime_params:
        digest.update(b"\0")
        digest.update(
            json.dumps(
                dict(sorted(runtime_params.items())),
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        )
    return digest.hexdigest()[:16]


def job_state_path(
    config_path: Path,
    job_id: str,
    *,
    runtime_params: Mapping[str, str] | None = None,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> Path:
    """Return the state.json path for one configuration identity."""
    identity = job_identity_hash(config_path, job_id, runtime_params=runtime_params)
    return state_root / f"{job_id}-{identity}" / "state.json"
