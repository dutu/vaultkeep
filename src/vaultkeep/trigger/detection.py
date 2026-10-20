"""Observation of one configured instance's running state.

Detection fails closed: a strong identity match without a resolvable data-directory
predicate is reported as ambiguous, never as stopped, so it can never arm a backup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

import psutil

from vaultkeep.trigger.config import InstanceConfig, MatcherSpec
from vaultkeep.trigger.matcher import (
    MatchResult,
    ProcessInfo,
    match_identity,
    required_env_keys,
)
from vaultkeep.trigger.models import Observation
from vaultkeep.trigger.paths import expand_user_path
from vaultkeep.trigger.process import ProcessProvider, boot_id

_DIRECTORY_ENV_KEY: dict[str, str] = {
    "electrum": "ELECTRUMDIR",
    "wasabi": "WASABI_DATADIR",
}
_DIRECTORY_OPTIONS: dict[str, tuple[str, ...]] = {
    "electrum": ("-D", "--dir"),
    "wasabi": ("--datadir",),
    "bisq": ("--appDataDir",),
}


@dataclass(frozen=True, slots=True)
class DetectionResult:
    observation: Observation
    boot_id: str | None = None
    pid: int | None = None
    create_time: float | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    reason: str | None = None


def _canonical(value: str) -> str:
    return os.path.realpath(expand_user_path(value))


def _option_value(cmdline: tuple[str, ...], options: tuple[str, ...]) -> str | None:
    for index, token in enumerate(cmdline):
        for option in options:
            if token == option and index + 1 < len(cmdline):
                return cmdline[index + 1]
            if token.startswith(f"{option}="):
                return token[len(option) + 1 :]
    return None


def _resolve_directory(
    app: str, proc: ProcessInfo, provider: ProcessProvider
) -> tuple[str | None, str]:
    """Return (canonical directory, status) where status is resolved/unresolved/inaccessible."""
    env_key = _DIRECTORY_ENV_KEY.get(app)
    if env_key is not None:
        env = provider.read_env(proc.pid, [env_key])
        if env is None:
            return None, "inaccessible"
        if env.get(env_key):
            return _canonical(env[env_key]), "resolved"

    value = _option_value(proc.cmdline, _DIRECTORY_OPTIONS.get(app, ()))
    if value:
        return _canonical(value), "resolved"
    return None, "unresolved"


def detect(instance: InstanceConfig, provider: ProcessProvider) -> DetectionResult:
    """Return the instance's observation from a fresh process snapshot."""
    return detect_spec(
        app=instance.app,
        data_dir=str(instance.data_dir),
        matcher=instance.matcher,
        provider=provider,
    )


def detect_spec(
    *,
    app: str,
    data_dir: str,
    matcher: MatcherSpec,
    provider: ProcessProvider,
) -> DetectionResult:
    """Return an observation from a frozen app/data-dir/matcher snapshot."""
    current_boot = boot_id()
    wanted_dir = _canonical(data_dir)
    try:
        snapshot = provider.snapshot()
    except (psutil.Error, OSError):
        return DetectionResult(
            Observation.INSPECTION_UNAVAILABLE,
            boot_id=current_boot,
            reason="process snapshot is not readable",
        )

    identity_matches: list[ProcessInfo] = []
    ambiguous_reason: str | None = None
    unavailable_reason: str | None = None

    env_keys = required_env_keys(matcher)
    for proc in snapshot:
        # Reject known unrelated identities before reading any environment.
        identity = match_identity(replace(matcher, env_eq={}), proc)
        if identity.unavailable:
            unavailable_reason = identity.reason
            continue
        if not identity.matched:
            continue
        env: dict[str, str] | None = None
        if env_keys:
            env = provider.read_env(proc.pid, env_keys)
            if env is None:
                unavailable_reason = "process environment is not readable"
                continue
        candidate = replace(proc, env=env)
        result: MatchResult = match_identity(matcher, candidate)
        if result.matched:
            identity_matches.append(candidate)

    resolved_matches: list[ProcessInfo] = []
    for candidate in identity_matches:
        directory, status = _resolve_directory(app, candidate, provider)
        if status == "inaccessible":
            unavailable_reason = "data directory metadata is not readable"
            continue
        if status == "unresolved":
            ambiguous_reason = "could not resolve the instance data directory"
            continue
        if directory == wanted_dir:
            resolved_matches.append(candidate)

    # Re-validate identity immediately before reporting running.
    for candidate in resolved_matches:
        alive = provider.alive(candidate.pid, candidate.create_time)
        if not alive:
            unavailable_reason = "matching process disappeared, changed, or cannot be revalidated"
        if alive:
            return DetectionResult(
                observation=Observation.RUNNING,
                boot_id=current_boot,
                pid=candidate.pid,
                create_time=candidate.create_time,
                evidence={
                    "matched_pid": candidate.pid,
                    "create_time": candidate.create_time,
                    "data_dir": wanted_dir,
                },
            )

    if ambiguous_reason is not None:
        return DetectionResult(
            observation=Observation.AMBIGUOUS,
            boot_id=current_boot,
            reason=ambiguous_reason,
        )
    if unavailable_reason is not None:
        return DetectionResult(
            observation=Observation.INSPECTION_UNAVAILABLE,
            boot_id=current_boot,
            reason=unavailable_reason,
        )
    return DetectionResult(
        observation=Observation.STOPPED,
        boot_id=current_boot,
        evidence={"data_dir": wanted_dir},
    )
