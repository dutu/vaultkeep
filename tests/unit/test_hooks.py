"""Tests for lifecycle-hook execution behavior."""

from __future__ import annotations

import logging
import os
import shlex
import socket
import stat
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vaultkeep import hooks
from vaultkeep.config.models import HookConfig
from vaultkeep.destination import render_backup_base_name
from vaultkeep.errors import HookError


def _context() -> hooks.HookContext:
    return hooks.HookContext(job_id="app", config_path=Path("/etc/vaultkeep/jobs/app.yaml"))


@pytest.fixture
def trusted_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execution tests do not depend on sandbox-specific filesystem ownership."""
    monkeypatch.setattr(hooks, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))


def test_hook_uses_fixed_environment(monkeypatch: pytest.MonkeyPatch, trusted_shell: None) -> None:
    monkeypatch.setenv("VAULTKEEP_TEST_SECRET", "must not be inherited")
    hook = HookConfig(
        command='printf "%s\\n" "$VAULTKEEP_JOB" "${VAULTKEEP_TEST_SECRET-unset}" "$PWD"; '
        "if read -r line; then exit 1; fi",
        timeout_seconds=5,
    )

    execution = hooks.run_hook("before_check", hook, _context())

    assert execution.stdout.splitlines() == [b"app", b"unset", b"/"]
    assert execution.outcome.exit_code == 0


def test_hook_failure_is_reported(trusted_shell: None) -> None:
    hook = HookConfig(command="exit 4", timeout_seconds=5)

    with pytest.raises(HookError, match="status 4"):
        hooks.require_success(hooks.run_hook("before_archive", hook, _context()))


def test_hook_timeout_is_reported(trusted_shell: None, caplog: pytest.LogCaptureFixture) -> None:
    hook = HookConfig(command="printf 'before timeout'; sleep 5", timeout_seconds=1)
    caplog.set_level(logging.INFO, logger="vaultkeep.hooks")

    execution = hooks.run_hook("after_archive", hook, _context(), include_command_output=True)

    assert execution.outcome.timed_out is True
    assert "exit_code=None timed_out=True" in caplog.text
    assert "after_archive hook stdout: before timeout" in caplog.text
    with pytest.raises(HookError, match="after_archive hook timed out"):
        hooks.require_success(execution)


@pytest.mark.skipif(os.name != "posix", reason="process-group semantics are POSIX-specific")
def test_timeout_terminates_hook_descendants(tmp_path: Path, trusted_shell: None) -> None:
    """A timed-out hook cannot leave a child process behind."""
    pid_file = tmp_path / "child.pid"
    child = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(30)"
    )
    hook = HookConfig(command=shlex.join([sys.executable, "-c", parent]), timeout_seconds=1)

    execution = hooks.run_hook("before_archive", hook, _context())

    assert execution.outcome.timed_out is True
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        status_file = Path(f"/proc/{child_pid}/status")
        if status_file.exists() and "State:\tZ" in status_file.read_text(encoding="utf-8"):
            break
        time.sleep(0.05)
    else:
        pytest.fail("timed-out hook left its descendant process running")


def test_hook_output_is_bounded(trusted_shell: None, caplog: pytest.LogCaptureFixture) -> None:
    hook = HookConfig(
        command=shlex.join(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x' * 1100000 + 'stdout-tail'); "
                "sys.stderr.write('y' * 1100000 + 'stderr-tail')",
            ]
        ),
        timeout_seconds=5,
    )
    caplog.set_level(logging.INFO, logger="vaultkeep.hooks")

    execution = hooks.run_hook("on_success", hook, _context(), include_command_output=True)

    assert execution.outcome.stdout_truncated is True
    assert execution.outcome.stderr_truncated is True
    assert len(execution.stdout) == 1024 * 1024
    assert len(execution.stderr) == 1024 * 1024
    assert "stdout_truncated=True stderr_truncated=True" in caplog.messages[0]
    assert caplog.messages[1] == "on_success hook stdout: " + "x" * (1024 * 1024)
    assert caplog.messages[2] == "on_success hook stderr: " + "y" * (1024 * 1024)


@pytest.mark.parametrize("message", ["$VAULTKEEP_JOB on $(hostname)", "{job} on {hostname}"])
def test_notification_command_expands_job_and_hostname(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, trusted_shell: None, message: str
) -> None:
    notifier = tmp_path / "telegram-notify"
    notifier.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    notifier.chmod(0o700)
    monkeypatch.setitem(hooks._BASE_ENVIRONMENT, "PATH", f"{tmp_path}:/usr/bin:/bin")
    hook = HookConfig(command=f'telegram-notify --level error "Backup failed for {message}"')

    execution = hooks.run_hook("on_failure", hook, _context())

    assert execution.outcome.exit_code == 0
    assert execution.stdout.decode().splitlines() == [
        "--level",
        "error",
        f"Backup failed for app on {socket.gethostname()}",
    ]


def test_shell_builtins_pipelines_and_redirection(tmp_path: Path, trusted_shell: None) -> None:
    output = tmp_path / "message.txt"
    hook = HookConfig(
        command=f"cd {shlex.quote(str(tmp_path))} && export MESSAGE=hello; "
        'printf "%s\\n" "$MESSAGE" | tr a-z A-Z > message.txt; cat message.txt'
    )

    execution = hooks.run_hook("on_success", hook, _context())

    assert execution.outcome.exit_code == 0
    assert execution.stdout == b"HELLO\n"
    assert output.read_text(encoding="utf-8") == "HELLO\n"


def test_context_variable_contents_are_not_reinterpreted_as_shell_code(
    tmp_path: Path, trusted_shell: None
) -> None:
    marker = tmp_path / "should-not-exist"
    error = f'$(touch {marker}); "quotes" and `commands`'
    hook = HookConfig(command='printf "%s" "$VAULTKEEP_ERROR"')

    execution = hooks.run_hook("on_failure", hook, replace(_context(), error=error))

    assert execution.outcome.exit_code == 0
    assert execution.stdout.decode() == error
    assert not marker.exists()


@pytest.mark.parametrize("command", ["if then", "vaultkeep-command-that-does-not-exist"])
def test_shell_syntax_and_missing_command_failures_are_reported(
    command: str, trusted_shell: None
) -> None:
    execution = hooks.run_hook("before_check", HookConfig(command=command), _context())

    assert execution.stderr
    with pytest.raises(HookError, match="exited with status"):
        hooks.require_success(execution)


def test_user_mode_allows_root_owned_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {"owner_uid": os.geteuid(), "owner_gid": None, "allow_root_owned": True}

    def validate(**kwargs: object) -> Path:
        assert kwargs == expected
        return Path("/bin/sh")

    monkeypatch.setattr(hooks, "validate_hook_shell", validate)
    execution = hooks.run_hook(
        "on_success",
        HookConfig(command="printf hello"),
        _context(),
        owner_uid=os.geteuid(),
        owner_gid=None,
        allow_root_owned=True,
    )

    assert execution.outcome.exit_code == 0
    assert execution.stdout == b"hello"


def test_shell_validation_accepts_system_symlinks() -> None:
    # Sandbox mounts can map system ownership to an unmapped UID/GID.
    status = Path("/bin/sh").stat()

    resolved = hooks.validate_hook_shell(owner_uid=status.st_uid, owner_gid=status.st_gid)

    assert resolved == Path("/bin/sh").resolve()


@pytest.mark.parametrize("mode", [stat.S_IFREG | 0o777, stat.S_IFDIR | 0o777])
def test_shell_validation_rejects_writable_targets(
    monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    status = os.stat_result((mode, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(Path, "stat", lambda *args, **kwargs: status)
    monkeypatch.setattr(Path, "lstat", lambda *args, **kwargs: status)

    with pytest.raises(HookError, match="not be writable"):
        hooks.validate_hook_shell()


def test_shell_validation_rejects_untrusted_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 1, 12345, 12345, 0, 0, 0, 0))
    monkeypatch.setattr(Path, "lstat", lambda *args, **kwargs: status)

    with pytest.raises(HookError, match="owned by root"):
        hooks.validate_hook_shell()


def test_hook_template_values_and_formats_match_destination(trusted_shell: None) -> None:
    context = replace(
        _context(),
        hostname="backup-host",
        timestamp=datetime(2026, 10, 20, 21, 22, 23, tzinfo=timezone(timedelta(hours=3))),
        source_digest="sha256:" + "a" * 64,
        archive_format="tar.zst",
    )
    template = "{job}-{hostname}-{timestamp:%H%M}-{timestamp_utc:%H%M}-{source_hash:.12}-{format}"
    hook = HookConfig(command=f'printf "%s\\n" "{template}" "{{timestamp}}" "{{timestamp_utc}}"')

    execution = hooks.run_hook("on_success", hook, context)
    expected_name = render_backup_base_name(
        template,
        job_id=context.job_id,
        hostname=context.hostname,
        created_at=context.timestamp,
        source_digest=context.source_digest,
        archive_format=context.archive_format,
    )

    assert execution.outcome.exit_code == 0
    assert execution.stdout.decode().splitlines() == [
        expected_name,
        "2026-10-20 21:22:23+03:00",
        "2026-10-20 18:22:23+00:00",
    ]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('printf "%s" {job}', "app"),
        ('printf "%s" "prefix-{job}-suffix"', "prefix-app-suffix"),
        ("printf '%s' '{job}'", "{job}"),
        (r"printf '%s' \{job}", "{job}"),
        ('''printf "%s" "'{job}'"''', "'app'"),
        (r'''printf "%s" "\"{job}\""''', '"app"'),
        ('''printf "%s" "$(printf '%s' '{job}')"''', "{job}"),
        ('''printf "%s" "$(printf "%s" "{job}")"''', "app"),
        ('''printf "%s" "`printf '%s' '{job}'`"''', "{job}"),
        ('''job=shell; printf '%s' "${job}:{job}:${MISSING:-{job}}"''', "shell:app:app"),
        ("""show() { printf '%s' "{job}"; }; show""", "app"),
        ('''printf '%s' "{unknown} {formatting}"''', "{unknown} {formatting}"),
        ('''# {job:bad}\nprintf '%s' "{job}"''', "app"),
        ('''cat <<EOF\n{job} $VAULTKEEP_JOB\nEOF\nprintf '%s' "{job}"''', "app app\napp"),
        (
            '''cat <<'EOF'\n{job} $VAULTKEEP_JOB\nEOF\nprintf '%s' "{job}"''',
            "{job} $VAULTKEEP_JOB\napp",
        ),
        ('''cat <<'{job}'\nbody\n{job}\nprintf '%s' "{job}"''', "body\napp"),
        ('''cat <<A <<-B\nignored\nA\n\t{job}\n\tB\nprintf '%s' "{job}"''', "app\napp"),
        ("""cat <<EOF\n'{job}' "{job}" # {job}\nEOF""", "'app' \"app\" # app\n"),
        ("""cat <<EOF\n$(printf '%s' '{job}')\nEOF""", "{job}\n"),
        ('''printf '%s' "$((1 << 2))"\nprintf '%s' '{job}' "{job}"''', "4{job}app"),
    ],
)
def test_hook_templates_preserve_shell_quoting_and_syntax(
    command: str, expected: str, trusted_shell: None
) -> None:
    execution = hooks.run_hook("on_success", HookConfig(command=command), _context())

    assert execution.outcome.exit_code == 0, execution.stderr
    assert execution.stdout.decode() == expected


def test_hook_template_values_are_not_shell_code(tmp_path: Path, trusted_shell: None) -> None:
    marker = tmp_path / "should-not-exist"
    hostname = f'host"; touch {marker}; echo "$(touch {marker}) `touch {marker}`'
    context = replace(_context(), hostname=hostname)

    execution = hooks.run_hook(
        "on_success", HookConfig(command='printf "%s" "{hostname}"'), context
    )

    assert execution.outcome.exit_code == 0
    assert execution.stdout.decode() == hostname
    assert not marker.exists()


def test_hook_template_source_hash_is_empty_before_hashing(trusted_shell: None) -> None:
    execution = hooks.run_hook(
        "before_check", HookConfig(command='printf "[%s]" "{source_hash:.12}"'), _context()
    )

    assert execution.outcome.exit_code == 0
    assert execution.stdout == b"[]"
