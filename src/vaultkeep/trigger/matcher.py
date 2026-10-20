"""Process identity matching against a strong matcher specification."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from vaultkeep.trigger.config import MatcherSpec


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """A sanitized snapshot of one process."""

    pid: int
    create_time: float
    exe_realpath: str | None
    cmdline: tuple[str, ...]
    env: dict[str, str] | None = None
    cmdline_available: bool = True


@dataclass(frozen=True, slots=True)
class MatchResult:
    matched: bool
    reason: str | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    unavailable: bool = False


def _canonical(value: str) -> str:
    return os.path.realpath(value)


def _match_entrypoint(spec_value: str, kind: str, cmdline: tuple[str, ...]) -> bool:
    if kind == "script":
        wanted = _canonical(spec_value)
        for token in cmdline[:2]:
            if _canonical(token) == wanted:
                return True
        return False
    if kind == "jar":
        wanted = _canonical(spec_value)
        for index, token in enumerate(cmdline):
            if token == "-jar" and index + 1 < len(cmdline):
                if _canonical(cmdline[index + 1]) == wanted:
                    return True
            elif _canonical(token) == wanted:
                return True
        return False
    # java_main: exact entry token.
    return spec_value in cmdline


def match_identity(matcher: MatcherSpec, proc: ProcessInfo) -> MatchResult:
    """Evaluate a matcher's strong identity and supporting predicates."""
    unavailable = False
    if matcher.exe_realpath is not None:
        if proc.exe_realpath is None:
            unavailable = True
        elif _canonical(proc.exe_realpath) != _canonical(matcher.exe_realpath):
            return MatchResult(False, "executable path does not match")

    if matcher.entrypoints:
        if not proc.cmdline_available:
            unavailable = True
        elif not any(
            _match_entrypoint(entrypoint.value, entrypoint.kind, proc.cmdline)
            for entrypoint in matcher.entrypoints
        ):
            return MatchResult(False, "no configured entrypoint matches")

    if matcher.exe_realpath is None and not matcher.entrypoints:
        return MatchResult(False, "matcher declares no strong identity")

    if not proc.cmdline_available and (matcher.argv_tokens or matcher.cmdline_regex):
        unavailable = True
    missing = (
        [token for token in matcher.argv_tokens if token not in proc.cmdline]
        if proc.cmdline_available
        else []
    )
    if missing:
        return MatchResult(False, "argv tokens not present")

    if matcher.cmdline_regex is not None and proc.cmdline_available:
        pattern = re.compile(matcher.cmdline_regex)
        if not any(pattern.fullmatch(token) for token in proc.cmdline):
            return MatchResult(False, "cmdline regex did not match any token")

    if unavailable:
        return MatchResult(False, "process identity metadata is not readable", unavailable=True)

    if matcher.env_eq:
        if proc.env is None:
            return MatchResult(False, "environment not read", unavailable=True)
        for key, value in matcher.env_eq.items():
            if proc.env.get(key) != value:
                return MatchResult(False, f"environment key {key} does not match")

    return MatchResult(
        True,
        evidence={"pid": proc.pid, "create_time": proc.create_time},
    )


def required_env_keys(matcher: MatcherSpec) -> tuple[str, ...]:
    """Return environment keys the matcher needs to read for a process."""
    return tuple(sorted(matcher.env_eq))
