"""Tests for lifecycle-hook execution behavior."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from vaultkeep import hooks
from vaultkeep.config.models import HookConfig
from vaultkeep.errors import HookError


def _context() -> hooks.HookContext:
    return hooks.HookContext(job_id="app", config_path=Path("/etc/vaultkeep/jobs/app.yaml"))


def test_hook_uses_fixed_environment_and_no_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks, "validate_hook_executable", lambda command: Path(command[0]))
    hook = HookConfig(
        command=[sys.executable, "-c", "import os; print(os.environ['VAULTKEEP_JOB'])"],
        timeout_seconds=5,
    )

    execution = hooks.run_hook("before_check", hook, _context())

    assert execution.stdout.strip() == b"app"
    assert execution.outcome.exit_code == 0


def test_hook_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks, "validate_hook_executable", lambda command: Path(command[0]))
    hook = HookConfig(command=[sys.executable, "-c", "raise SystemExit(4)"], timeout_seconds=5)

    with pytest.raises(HookError, match="status 4"):
        hooks.require_success(hooks.run_hook("before_archive", hook, _context()))


def test_hook_timeout_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks, "validate_hook_executable", lambda command: Path(command[0]))
    hook = HookConfig(
        command=[sys.executable, "-c", "import time; time.sleep(5)"], timeout_seconds=1
    )

    execution = hooks.run_hook("after_archive", hook, _context())

    assert execution.outcome.timed_out is True


@pytest.mark.skipif(os.name != "posix", reason="process-group semantics are POSIX-specific")
def test_timeout_terminates_hook_descendants(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A timed-out hook cannot leave a child process behind."""
    monkeypatch.setattr(hooks, "validate_hook_executable", lambda command: Path(command[0]))
    pid_file = tmp_path / "child.pid"
    child = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(30)"
    )
    hook = HookConfig(command=[sys.executable, "-c", parent], timeout_seconds=1)

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


def test_hook_output_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks, "validate_hook_executable", lambda command: Path(command[0]))
    hook = HookConfig(
        command=[sys.executable, "-c", "import sys; sys.stdout.write('x' * 1100000)"],
        timeout_seconds=5,
    )

    execution = hooks.run_hook("on_success", hook, _context())

    assert execution.outcome.stdout_truncated is True
    assert len(execution.stdout) == 1024 * 1024
