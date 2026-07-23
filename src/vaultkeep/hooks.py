"""Secure direct lifecycle-hook validation and execution."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from vaultkeep.config.models import HookConfig
from vaultkeep.errors import HookError
from vaultkeep.state.models import HookOutcomeState, HookPhase

_OUTPUT_LIMIT = 1024 * 1024
_BASE_ENVIRONMENT = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
}


@dataclass(frozen=True, slots=True)
class HookContext:
    """Non-secret facts exposed to one lifecycle hook."""

    job_id: str
    config_path: Path
    backup_id: str = ""
    source_digest: str = ""
    destination: str = ""
    archive: str = ""
    backup_directory: str = ""
    result: str = "running"
    failed_stage: str = ""
    error: str = ""
    version: str = ""


@dataclass(frozen=True, slots=True)
class HookExecution:
    """Non-secret hook result and bounded captured output."""

    outcome: HookOutcomeState
    stdout: bytes
    stderr: bytes


def validate_hook_executable(command: tuple[str, ...] | list[str]) -> Path:
    """Resolve one root-owned, non-writable executable and its parent path."""
    if not command or not Path(command[0]).is_absolute():
        raise HookError("Hook executable path must be absolute")
    configured = Path(command[0])
    _validate_path_chain(configured)
    try:
        resolved = configured.resolve(strict=True)
        status = resolved.stat()
    except OSError as error:
        raise HookError(f"Cannot resolve hook executable {configured}: {error}") from error
    _validate_path_chain(resolved)
    if not stat.S_ISREG(status.st_mode) or not os.access(resolved, os.X_OK):
        raise HookError(f"Hook executable is not a regular executable file: {resolved}")
    _validate_secure_status(status, resolved)
    _validate_shebang(resolved)
    return resolved


def run_hook(phase: HookPhase, hook: HookConfig, context: HookContext) -> HookExecution:
    """Execute one trusted hook directly with fixed environment and bounded output."""
    executable = validate_hook_executable(hook.command)
    environment = _hook_environment(phase, context)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            hook.command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            env=environment,
            shell=False,
            start_new_session=True,
        )
    except OSError as error:
        raise HookError(f"Cannot start {phase} hook {executable}: {error}") from error
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=hook.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_group(process)
        stdout, stderr = process.communicate()
    duration = time.monotonic() - started
    bounded_stdout, stdout_truncated = _bounded(stdout)
    bounded_stderr, stderr_truncated = _bounded(stderr)
    outcome = HookOutcomeState(
        phase=phase,
        duration_seconds=duration,
        exit_code=process.returncode if not timed_out else None,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )
    return HookExecution(outcome, bounded_stdout, bounded_stderr)


def require_success(execution: HookExecution) -> None:
    """Turn a timeout, signal, or non-zero result into the stable hook error."""
    outcome = execution.outcome
    if outcome.timed_out:
        raise HookError(f"{outcome.phase} hook timed out")
    if outcome.exit_code != 0:
        raise HookError(f"{outcome.phase} hook exited with status {outcome.exit_code}")


def _hook_environment(phase: str, context: HookContext) -> dict[str, str]:
    return {
        **_BASE_ENVIRONMENT,
        "VAULTKEEP_JOB": context.job_id,
        "VAULTKEEP_CONFIG": str(context.config_path),
        "VAULTKEEP_BACKUP_ID": context.backup_id,
        "VAULTKEEP_SOURCE_DIGEST": context.source_digest,
        "VAULTKEEP_DESTINATION": context.destination,
        "VAULTKEEP_ARCHIVE": context.archive,
        "VAULTKEEP_BACKUP_DIRECTORY": context.backup_directory,
        "VAULTKEEP_RESULT": context.result,
        "VAULTKEEP_STAGE": phase,
        "VAULTKEEP_FAILED_STAGE": context.failed_stage,
        "VAULTKEEP_ERROR": context.error,
        "VAULTKEEP_VERSION": context.version,
    }


def _validate_path_chain(path: Path) -> None:
    for parent in (path.parent, *path.parent.parents):
        try:
            _validate_secure_status(parent.lstat(), parent)
        except OSError as error:
            raise HookError(f"Cannot inspect hook path component {parent}: {error}") from error


def _validate_secure_status(status: os.stat_result, path: Path) -> None:
    if status.st_uid != 0 or status.st_gid != 0:
        raise HookError(f"Hook path must be owned by root:root: {path}")
    if stat.S_IMODE(status.st_mode) & 0o022:
        raise HookError(f"Hook path must not be writable by group or other users: {path}")


def _validate_shebang(path: Path) -> None:
    try:
        with path.open("rb") as executable:
            first_line = executable.readline(4096)
    except OSError as error:
        raise HookError(f"Cannot inspect hook executable {path}: {error}") from error
    if not first_line.startswith(b"#!"):
        return
    interpreter = first_line[2:].strip().split(maxsplit=1)[0].decode("utf-8", errors="strict")
    if interpreter == "/usr/bin/env":
        raise HookError("Hook shebang must not use /usr/bin/env")
    interpreter_path = Path(interpreter)
    if not interpreter_path.is_absolute():
        raise HookError("Hook shebang interpreter path must be absolute")
    _validate_path_chain(interpreter_path)
    try:
        _validate_secure_status(interpreter_path.stat(), interpreter_path)
    except OSError as error:
        raise HookError(f"Cannot inspect hook interpreter {interpreter_path}: {error}") from error


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    kill_group: object = vars(os).get("killpg")
    if not callable(kill_group):
        process.terminate()
        return
    try:
        kill_group(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        kill_group(process.pid, getattr(signal, "SIGKILL", 9))


def _bounded(value: bytes) -> tuple[bytes, bool]:
    return (value, False) if len(value) <= _OUTPUT_LIMIT else (value[:_OUTPUT_LIMIT], True)
