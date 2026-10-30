"""External notification delivery failures are optional; CLI mistakes remain errors."""

from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path

import psutil
import pytest
from ruamel.yaml import YAML

from tests.unit import test_workflow_cleanup as cleanup
from vaultkeep import hooks
from vaultkeep.backup import workflow
from vaultkeep.cli import main
from vaultkeep.cli.parser import create_parser
from vaultkeep.notifications import external

job = cleanup.job


def _tool(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}\n")
    path.chmod(0o700)


@pytest.fixture
def tools(tmp_path, monkeypatch, valid_config):
    directory = tmp_path / "tools"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    monkeypatch.delenv("VAULTKEEP_STAGE", raising=False)
    monkeypatch.delenv("VAULTKEEP_JOB", raising=False)
    valid_config["external_notifier"] = {"command": "telegram-notify"}
    config = tmp_path / "app.yaml"
    config.write_text(json.dumps(valid_config))
    monkeypatch.setenv("VAULTKEEP_CONFIG", str(config))
    return directory


def test_message_is_one_literal_argument_and_success_is_quiet(tools, capsys):
    arguments = tools / "arguments.json"
    _tool(
        tools / "telegram-notify",
        f"import json, sys\nfrom pathlib import Path\n"
        f"Path({str(arguments)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "print('noisy success'); print('more output', file=sys.stderr)",
    )
    marker = tools / "must-not-exist"
    message = f'-message; $(touch {marker}) "quotes"\nsecond line'

    assert main(["notify", "--level", "warning", "--", message]) == 0

    assert json.loads(arguments.read_text()) == ["--level", "warning", "--", message]
    assert not marker.exists()
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("status", [2, 4, -signal.SIGTERM])
def test_nonzero_delivery_warns_with_bounded_single_line_diagnostic(tools, capsys, status):
    _tool(
        tools / "telegram-notify",
        "import os, signal, sys\n"
        "sys.stderr.buffer.write(b'network unavailable\\n\\x1b[31m' + b'x' * 10000)\n"
        "sys.stderr.flush()\n"
        + ("os.kill(os.getpid(), signal.SIGTERM)" if status < 0 else f"sys.exit({status})"),
    )

    assert main(["notify", "hello"]) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "WARNING" in captured.err
    assert f"exited with status {status}" in captured.err
    assert "network unavailable" not in captured.err
    assert "\x1b" not in captured.err
    assert len(captured.err) < 500
    assert len(captured.err.splitlines()) == 1


@pytest.mark.parametrize("failure", ["missing", "permission"])
def test_execution_failure_warns_and_returns_zero(tools, capsys, failure):
    if failure == "permission":
        executable = tools / "telegram-notify"
        executable.write_text("cannot execute")
        executable.chmod(0o600)

    assert main(["notify", "hello"]) == 0

    assert "Cannot execute external notifier" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        ["notify-telegram", "hello"],
        ["--invalid", "notify", "hello"],
        ["--config"],
        ["--param"],
        ["--config", "app.yaml", "notify", "bad\0arg"],
    ],
)
def test_invalid_vaultkeep_arguments_are_usage_errors(monkeypatch, capsys, arguments):
    monkeypatch.setattr(external, "notify", lambda *a, **k: pytest.fail("must not deliver"))
    with pytest.raises(SystemExit) as caught:
        main(arguments)
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--level", "unknown", "hello"],
        ["one", "two"],
        ["--help"],
        ["--config", "provider.ini", "--timeout", "custom", "--user"],
        ["", " ", "a\nb", "--", "-x", "--level=error"],
    ],
)
def test_external_arguments_are_opaque_and_ordered(tools, arguments):
    recorded = tools / "arguments.json"
    _tool(
        tools / "telegram-notify",
        f"import json,sys; open({str(recorded)!r}, 'w').write(json.dumps(sys.argv[1:]))",
    )
    assert main(["notify", *arguments]) == 0
    assert json.loads(recorded.read_text()) == arguments


@pytest.mark.parametrize("leader_ignores_term", [False, True])
def test_timeout_kills_process_group_even_when_leader_exits(tools, capsys, leader_ignores_term):
    pids = tools / "pids"
    handler = "SIG_IGN" if leader_ignores_term else "SIG_DFL"
    _tool(
        tools / "telegram-notify",
        "import os, signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = os.fork()\n"
        "if child:\n"
        f"    signal.signal(signal.SIGTERM, signal.{handler})\n"
        f"    Path({str(pids)!r}).write_text(f'{{os.getpid()}} {{child}}')\n"
        "time.sleep(120)",
    )
    config_path = tools.parent / "app.yaml"
    document = json.loads(config_path.read_text())
    document["external_notifier"]["timeout_seconds"] = 0.5
    config_path.write_text(json.dumps(document))
    started = time.monotonic()
    try:
        assert main(["--user", "--config", str(config_path), "notify", "hello"]) == 0
        assert time.monotonic() - started < 5
        assert "timed out after 0.5 seconds" in capsys.readouterr().err
        process_ids = [int(value) for value in pids.read_text().split()]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            alive = [
                pid
                for pid in process_ids
                if psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
            ]
            if not alive:
                break
            time.sleep(0.01)
        assert not alive
    finally:
        if pids.exists():
            for pid in map(int, pids.read_text().split()):
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("logging_failure", ["nonzero", "timeout", "missing"])
def test_hook_logging_failure_never_changes_result(tools, monkeypatch, capsys, logging_failure):
    _tool(tools / "telegram-notify", "import sys; sys.exit(4)")
    if logging_failure != "missing":
        _tool(
            tools / "logger",
            "import time; time.sleep(120)"
            if logging_failure == "timeout"
            else "import sys; sys.exit(9)",
        )
    monkeypatch.setenv("VAULTKEEP_STAGE", "on_success")
    monkeypatch.setenv("VAULTKEEP_JOB", "test-job")

    assert main(["notify", "hello"]) == 0

    warning = capsys.readouterr().err
    assert "job=test-job phase=on_success" in warning
    assert "status 4" in warning


def test_delivery_failure_in_real_success_hook_preserves_backup_success(job, tools, monkeypatch):
    logged = tools / "warning.json"
    _tool(tools / "telegram-notify", "import sys; sys.stderr.write('offline'); sys.exit(4)")
    _tool(
        tools / "logger",
        f"import json, sys\nfrom pathlib import Path\n"
        f"Path({str(logged)!r}).write_text(json.dumps(sys.argv[1:]))",
    )
    _tool(tools / "vaultkeep", "from vaultkeep.cli import main\nraise SystemExit(main())")
    monkeypatch.setitem(hooks._BASE_ENVIRONMENT, "PATH", f"{tools}:/usr/bin:/bin")
    # The sandbox's /bin ownership differs from a production system's ownership.
    for module in (hooks, workflow):
        monkeypatch.setattr(module, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))
    document = json.loads(job.read_text())
    document["external_notifier"] = {"command": str(tools / "telegram-notify")}
    document["hooks"]["on_success"] = {
        "command": 'vaultkeep notify "Backup completed for {job} on {hostname}"',
        "timeout_seconds": 60,
    }
    job.write_text(json.dumps(document))

    result = cleanup._run(job)

    assert result.result == "created"
    assert result.archive_path.is_file()
    warning = json.loads(logged.read_text())
    assert warning[:5] == ["-t", "vaultkeep", "-p", "user.warning", "--"]
    assert "job=app phase=on_success" in warning[-1]
    assert "status 4" in warning[-1]
    assert "offline" not in warning[-1]


def test_sample_hook_commands_use_valid_helper_arguments():
    root = Path(__file__).resolve().parents[2]
    yaml = YAML(typ="safe")
    for path in sorted((root / "examples").glob("*.yaml")):
        section = (
            path.read_text()
            .split("## Lifecycle hooks", 1)[1]
            .split("## Desktop notifications", 1)[0]
        )
        text = "\n".join(line.removeprefix("# ") for line in section.splitlines()[1:])
        for config in yaml.load(text)["hooks"].values():
            hook = hooks.HookContext("app", path)
            command, _ = hooks.render_hook_command(config["command"], hook)
            arguments = shlex.split(command)
            assert arguments[0:2] == ["vaultkeep", "notify"]
            parsed = create_parser().parse_args(arguments[1:])
            assert parsed.arguments
            assert config["timeout_seconds"] > 15 + 4


def test_explicit_configuration_overrides_hook_environment(tools, capsys):
    config = tools.parent / "app.yaml"
    document = json.loads(config.read_text())
    _tool(tools / "selected", "import sys; sys.exit(6)")
    document["external_notifier"]["command"] = str(tools / "selected")
    explicit = tools.parent / "explicit.yaml"
    document["job"]["id"] = "explicit"
    explicit.write_text(json.dumps(document))
    assert main(["--user", "--config", str(explicit), "notify", "hello"]) == 0
    assert "status 6" in capsys.readouterr().err


def test_manual_relative_configuration_path(tools, monkeypatch):
    _tool(tools / "telegram-notify", "pass")
    monkeypatch.chdir(tools.parent)
    assert main(["--user", "--config", "app.yaml", "notify", "hello"]) == 0


def test_runtime_missing_notifier_catches_indirect_hook_use(tools, capsys):
    config = tools.parent / "app.yaml"
    document = json.loads(config.read_text())
    document.pop("external_notifier")
    document["hooks"]["on_success"] = {"command": "/path/to/custom-notification-script"}
    config.write_text(json.dumps(document))
    assert main(["notify", "hello"]) == 3
    assert "external_notifier is required" in capsys.readouterr().err


def test_missing_config_is_configuration_error(monkeypatch, capsys):
    monkeypatch.delenv("VAULTKEEP_CONFIG", raising=False)
    assert main(["--user", "notify", "hello"]) == 3
    assert "requires --config or VAULTKEEP_CONFIG" in capsys.readouterr().err


def test_manual_system_scope_requires_root(tools, monkeypatch, capsys):
    monkeypatch.delenv("VAULTKEEP_CONFIG")
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    assert main(["--config", str(tools.parent / "app.yaml"), "notify", "hello"]) == 3
    assert "use sudo or select --user" in capsys.readouterr().err


def test_notification_needs_no_backup_runtime_or_parameter_values(tools, monkeypatch):
    config = tools.parent / "app.yaml"
    document = json.loads(config.read_text())
    document["sources"][0]["path"] = "/missing/source"
    document["destination"]["root"] = "/unmounted/{NETWORK}"
    document["archive"]["format"] = "tar.7z"
    document["encryption"] = {"mode": "password", "password_file": "/missing/password"}
    document["application"] = {
        "name": "bisq",
        "user": "missing-account",
        "data_dir": "/missing/app",
        "matcher": {"entrypoints": [{"kind": "java_main", "value": "test.App"}]},
    }
    config.write_text(json.dumps(document))
    _tool(tools / "telegram-notify", "pass")
    monkeypatch.setattr(
        workflow, "discover_backups", lambda *a, **k: pytest.fail("destination access")
    )
    monkeypatch.setattr(
        workflow, "destination_lock", lambda *a, **k: pytest.fail("destination lock")
    )
    assert main(["--user", "--config", str(config), "notify", "hello"]) == 0
    assert not (tools.parent / "state").exists()


def test_unexpected_programming_error_is_not_success(tools, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("programming error")

    monkeypatch.setattr(external.subprocess, "Popen", broken)
    with pytest.raises(RuntimeError, match="programming error"):
        main(["notify", "hello"])
