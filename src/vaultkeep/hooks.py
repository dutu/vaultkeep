"""Shell lifecycle-hook validation and execution."""

from __future__ import annotations

import logging
import os
import re
import shlex
import signal
import socket
import stat
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from vaultkeep.config.models import HookConfig
from vaultkeep.config.runtime import DESTINATION_NAME_FIELDS, builtin_template_values
from vaultkeep.errors import HookError
from vaultkeep.state.models import HookOutcomeState, HookPhase

_OUTPUT_LIMIT = 1024 * 1024
_SHELL = Path("/bin/sh")
_TEMPLATE_FIELD = re.compile(
    r"\{(?P<field>" + "|".join(sorted(DESTINATION_NAME_FIELDS)) + r")(?![A-Za-z0-9_])"
)
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
    hostname: str = field(default_factory=socket.gethostname)
    timestamp: datetime = field(default_factory=lambda: datetime.now().astimezone())
    archive_format: str = ""


@dataclass(frozen=True, slots=True)
class HookExecution:
    """Non-secret hook result and bounded captured output."""

    outcome: HookOutcomeState
    stdout: bytes
    stderr: bytes


def validate_hook_shell(
    *,
    owner_uid: int = 0,
    owner_gid: int | None = 0,
    allow_root_owned: bool = False,
) -> Path:
    """Resolve the securely owned, non-writable shell and its parent path."""
    configured = _SHELL
    _validate_path_chain(
        configured,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        allow_root_owned=allow_root_owned,
    )
    try:
        _validate_secure_status(
            configured.lstat(),
            configured,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            allow_root_owned=allow_root_owned,
        )
        resolved = configured.resolve(strict=True)
        status = resolved.stat()
    except OSError as error:
        raise HookError(f"Cannot resolve hook executable {configured}: {error}") from error
    _validate_path_chain(
        resolved,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        allow_root_owned=allow_root_owned,
    )
    if not stat.S_ISREG(status.st_mode) or not os.access(resolved, os.X_OK):
        raise HookError(f"Hook executable is not a regular executable file: {resolved}")
    _validate_secure_status(
        status,
        resolved,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        allow_root_owned=allow_root_owned,
    )
    return resolved


def run_hook(
    phase: HookPhase,
    hook: HookConfig,
    context: HookContext,
    *,
    include_command_output: bool = False,
    owner_uid: int = 0,
    owner_gid: int | None = 0,
    allow_root_owned: bool = False,
) -> HookExecution:
    """Execute one trusted shell hook with fixed environment and bounded output."""
    if owner_uid == 0 and owner_gid == 0 and not allow_root_owned:
        executable = validate_hook_shell()
    else:
        executable = validate_hook_shell(
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            allow_root_owned=allow_root_owned,
        )
    environment = _hook_environment(phase, context)
    command, template_environment = render_hook_command(hook.command, context)
    environment.update(template_environment)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            [str(_SHELL), "-c", command],
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
    logger = logging.getLogger(__name__)
    logger.info(
        "%s hook: executable=%s duration_seconds=%.3f exit_code=%s timed_out=%s "
        "stdout_truncated=%s stderr_truncated=%s",
        phase,
        executable,
        duration,
        outcome.exit_code,
        timed_out,
        stdout_truncated,
        stderr_truncated,
    )
    if include_command_output:
        for stream_name, output in (("stdout", bounded_stdout), ("stderr", bounded_stderr)):
            if output:
                logger.info(
                    "%s hook %s: %s", phase, stream_name, output.decode("utf-8", errors="replace")
                )
    return HookExecution(outcome, bounded_stdout, bounded_stderr)


def require_success(execution: HookExecution) -> None:
    """Turn a timeout, signal, or non-zero result into the stable hook error."""
    outcome = execution.outcome
    if outcome.timed_out:
        raise HookError(f"{outcome.phase} hook timed out")
    if outcome.exit_code != 0:
        raise HookError(f"{outcome.phase} hook exited with status {outcome.exit_code}")


def validate_hook_template(command: str) -> None:
    """Validate recognized placeholders without executing any shell code."""
    render_hook_command(
        command,
        HookContext("job", Path("/"), hostname="host", timestamp=datetime(2026, 1, 1, tzinfo=UTC)),
    )


def render_hook_command(command: str, context: HookContext) -> tuple[str, dict[str, str]]:
    """Compile built-ins to shell expansions, keeping their values out of shell source."""
    values = builtin_template_values(
        job_id=context.job_id,
        hostname=context.hostname,
        timestamp=context.timestamp,
        source_hash=context.source_digest.removeprefix("sha256:"),
        archive_format=context.archive_format,
    )
    environment: dict[str, str] = {}
    return _render_hook_text(command, values, environment), environment


def _render_hook_text(
    command: str,
    values: Mapping[str, str | datetime],
    environment: dict[str, str],
    *,
    here_document: bool = False,
) -> str:
    """Keep shell quote boundaries while replacing recognized template fields."""
    result: list[str] = []
    quote = "heredoc" if here_document else ""
    substitutions: list[tuple[str, str]] = []
    heredocs: list[tuple[str, bool, bool]] = []
    literal_until = 0
    index = 0
    while index < len(command):
        character = command[index]
        if character == "\\":
            result.append(command[index : index + 2])
            index += 2
            continue
        if character == "'" and not quote:
            end = command.find("'", index + 1)
            end = len(command) if end == -1 else end + 1
            result.append(command[index:end])
            index = end
            continue
        if not quote and character == "#" and (index == 0 or command[index - 1] in " \t\n;|&()"):
            end = command.find("\n", index)
            end = len(command) if end == -1 else end
            result.append(command[index:end])
            index = end
            continue
        if character == '"' and quote in {"", '"'}:
            quote = "" if quote == '"' else '"'
        elif command.startswith("$((", index):
            substitutions.append((quote, "arithmetic"))
            quote = ""
            result.append("$((")
            index += 3
            continue
        elif command.startswith("$(", index):
            substitutions.append((quote, ")"))
            quote = ""
            result.append("$(")
            index += 2
            continue
        elif character == "(" and not quote:
            substitutions.append((quote, ")"))
        elif character == "`":
            if substitutions and substitutions[-1][1] == "`" and not quote:
                quote, _ = substitutions.pop()
            else:
                substitutions.append((quote, "`"))
                quote = ""
        elif (
            not quote
            and substitutions
            and substitutions[-1][1] == "arithmetic"
            and command.startswith("))", index)
        ):
            quote, _ = substitutions.pop()
            result.append("))")
            index += 2
            continue
        elif character == ")" and not quote and substitutions and substitutions[-1][1] == ")":
            quote, _ = substitutions.pop()
        elif command.startswith("${", index):
            result.append("${")
            index += 2
            continue
        elif (
            not quote
            and command.startswith("<<", index)
            and not any(end == "arithmetic" for _, end in substitutions)
        ):
            strip_tabs = command.startswith("<<-", index)
            start = index + (3 if strip_tabs else 2)
            stream = StringIO(command[start:])
            lexer = shlex.shlex(stream, posix=True, punctuation_chars=";&|()<>")
            lexer.whitespace_split = True
            try:
                delimiter = lexer.get_token()
            except ValueError:
                delimiter = None  # Let the shell report malformed quoting.
            if delimiter is not None:
                literal_until = start + stream.tell()
                expand = not any(char in command[start:literal_until] for char in "'\"\\")
                heredocs.append((delimiter, strip_tabs, expand))
        elif character == "\n" and heredocs:
            result.append(character)
            index += 1
            for delimiter, strip_tabs, expand in heredocs:
                body: list[str] = []
                closing = ""
                while index < len(command):
                    end = command.find("\n", index)
                    end = len(command) if end == -1 else end + 1
                    line = command[index:end]
                    index = end
                    if (line.lstrip("\t") if strip_tabs else line).removesuffix("\n") == delimiter:
                        closing = line
                        break
                    body.append(line)
                text = "".join(body)
                result.append(
                    _render_hook_text(text, values, environment, here_document=True)
                    if expand
                    else text
                )
                result.append(closing)
            heredocs.clear()
            continue
        match = _TEMPLATE_FIELD.match(command, index) if index >= literal_until else None
        if match is not None:
            end = command.find("}", match.end())
            if end == -1:
                raise HookError("Unclosed hook template placeholder")
            suffix = command[match.end() : end]
            if "{" in suffix or suffix.startswith("!"):
                raise HookError("Hook template nesting and conversions are not supported")
            field_name = match.group("field")
            spec = suffix.removeprefix(":")
            if spec and not (
                field_name in {"timestamp", "timestamp_utc"}
                or (field_name == "source_hash" and re.fullmatch(r"\.[1-9]\d*", spec))
            ):
                raise HookError(f"Invalid format specifier for hook placeholder {{{field_name}}}")
            try:
                value = format(values[field_name], spec)
            except ValueError as error:
                raise HookError(
                    f"Cannot format hook placeholder {{{field_name}}}: {error}"
                ) from error
            name = f"VAULTKEEP_TEMPLATE_{len(environment)}"
            environment[name] = value
            result.append("${" + name + "}")
            index = end + 1
            continue
        result.append(character)
        index += 1
    return "".join(result)


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


def _validate_path_chain(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int | None,
    allow_root_owned: bool,
) -> None:
    for parent in (path.parent, *path.parent.parents):
        try:
            _validate_secure_status(
                parent.lstat(),
                parent,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
                allow_root_owned=allow_root_owned,
            )
        except OSError as error:
            raise HookError(f"Cannot inspect hook path component {parent}: {error}") from error


def _validate_secure_status(
    status: os.stat_result,
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int | None,
    allow_root_owned: bool,
) -> None:
    owner_matches = status.st_uid == owner_uid and (owner_gid is None or status.st_gid == owner_gid)
    root_matches = allow_root_owned and status.st_uid == 0
    if not owner_matches and not root_matches:
        owner = _owner_label(owner_uid, owner_gid)
        raise HookError(f"Hook path must be owned by {owner}: {path}")
    if not stat.S_ISLNK(status.st_mode) and stat.S_IMODE(status.st_mode) & 0o022:
        raise HookError(f"Hook path must not be writable by group or other users: {path}")


def _owner_label(owner_uid: int, owner_gid: int | None) -> str:
    if owner_uid == 0 and owner_gid == 0:
        return "root:root"
    return f"{owner_uid}:{owner_gid}" if owner_gid is not None else str(owner_uid)


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
