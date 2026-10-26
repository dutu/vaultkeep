"""Tests for command parsing and public error mapping."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from vaultkeep import cli
from vaultkeep.backup import workflow
from vaultkeep.cli.commands import main
from vaultkeep.config import JobConfig
from vaultkeep.destination import finalize as finalize_module
from vaultkeep.errors import DestinationError
from vaultkeep.progress import VerificationProgress

EXAMPLE_CONFIG = Path(__file__).parents[2] / "examples" / "vaultkeep-schedule.yaml"


@pytest.mark.parametrize("entrypoint", ["console", "module"])
def test_ctrl_c_exits_without_traceback(entrypoint: str) -> None:
    script = """
import runpy
import signal
import sys
from vaultkeep.cli import main, commands

def interrupt(*args, **kwargs):
    signal.raise_signal(signal.SIGINT)

commands.run_backup = interrupt
sys.argv = ["vaultkeep", "--config", "app.yaml", "run"]
"""
    script += (
        'runpy.run_module("vaultkeep", run_name="__main__")'
        if entrypoint == "module"
        else "raise SystemExit(main())"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )

    assert result.returncode == 130
    assert result.stdout == ""
    assert result.stderr.endswith("Interrupted.\n")
    assert "Traceback" not in result.stderr
    assert "KeyboardInterrupt" not in result.stderr


def test_ctrl_c_during_startup_exits_without_traceback(monkeypatch, capsys) -> None:
    def interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "create_parser", interrupt)

    assert cli.main(["--version"]) == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Interrupted.\n"


def test_version_does_not_require_a_command(capsys: Any) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip()


def test_schema_only_validation_accepts_disabled_template(capsys: Any) -> None:
    assert main(["--config", str(EXAMPLE_CONFIG), "validate", "--schema-only"]) == 0
    output = capsys.readouterr().out
    assert "  Configuration   VALID" in output
    assert "  Schedule  DISABLED\n  Trigger   DISABLED" in output
    assert "Runtime checks  skipped (--schema-only)" in output


def test_schema_only_validation_uses_config_path(tmp_path: Path, capsys: Any) -> None:
    config = tmp_path / "app.yaml"
    lines = ["config_version: 2", "job:", "  id: app"]
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert main(["--config", str(config), "validate", "--schema-only"]) == 3
    assert "Configuration contains" in capsys.readouterr().err


def test_invalid_command_configuration_maps_to_exit_three(tmp_path: Path, capsys: Any) -> None:
    config = tmp_path / "app.yaml"
    config.write_text("not: a-complete-config\n", encoding="utf-8")

    assert main(["--config", str(config), "validate", "--schema-only"]) == 3
    assert "Configuration contains" in capsys.readouterr().err


def test_runtime_validation_probes_destination_writability(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_data = {
        **valid_config,
        "destination": {
            **valid_config["destination"],
            "root": str(tmp_path),
        },
    }
    config = JobConfig.model_validate(config_data)

    def fail_tempfile(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(finalize_module.tempfile, "mkdtemp", fail_tempfile)

    with pytest.raises(DestinationError, match="cannot create a probe directory: denied"):
        workflow._validate_runtime(
            config,
            require_sources=False,
            require_writable_destination=True,
        )


@pytest.fixture
def local_job(tmp_path: Path, valid_config: dict[str, Any]) -> Path:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    valid_config["sources"][0]["path"] = str(source)
    valid_config["destination"]["root"] = str(destination)
    config = tmp_path / "app.yaml"
    config.write_text(json.dumps(valid_config), encoding="utf-8")
    return config


def test_full_validation_probes_destination_and_leaves_no_artifacts(
    local_job: Path, capsys: Any
) -> None:
    assert main(["--config", str(local_job), "validate"]) == 0
    output = capsys.readouterr().out
    assert "  Backup paths   OK" in output
    assert "  Schedule  DISABLED\n  Trigger   DISABLED" in output
    assert "Result:  OK" in output
    assert list((local_job.parent / "destination").iterdir()) == []


@pytest.mark.parametrize("command", ["validate", "run"])
def test_validation_and_backup_reject_denied_atomic_rename_before_archiving(
    local_job: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, command: str
) -> None:
    def deny_rename(source: Path, destination: Path) -> None:
        raise PermissionError("rename denied")

    def unexpected_archive(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Archive creation must not start after a failed destination probe")

    monkeypatch.setattr(finalize_module, "_rename_noreplace", deny_rename)
    monkeypatch.setattr(workflow, "build_archive", unexpected_archive)
    monkeypatch.setenv("XDG_STATE_HOME", str(local_job.parent / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(local_job.parent / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(local_job.parent / "runtime"))

    assert main(["--user", "--config", str(local_job), command]) == 5
    error = capsys.readouterr().err
    assert "cannot atomically rename a probe directory: rename denied" in error
    assert str(local_job.parent / "destination") in error
    assert list((local_job.parent / "destination").iterdir()) == []


def test_schema_only_validation_never_probes_destination(
    local_job: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_probe(root: Path) -> None:
        pytest.fail("Schema-only validation must not probe the destination")

    def unexpected_mount_check(*args: Any, **kwargs: Any) -> str:
        pytest.fail("Schema-only validation must not inspect mounts")

    data = json.loads(local_job.read_text())
    data["destination"]["expected_fstype"] = "cifs"
    local_job.write_text(json.dumps(data))
    monkeypatch.setattr(workflow, "probe_destination_writability", unexpected_probe)
    monkeypatch.setattr(workflow, "_mounted_filesystem_type", unexpected_mount_check)
    (local_job.parent / "destination").rmdir()

    assert main(["--config", str(local_job), "validate", "--schema-only"]) == 0


@pytest.mark.parametrize("command", [["list"], ["verify"], ["prune", "--dry-run"]])
def test_read_only_commands_never_probe_destination(
    local_job: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    def unexpected_probe(root: Path) -> None:
        pytest.fail("Read-only commands must not probe the destination")

    monkeypatch.setattr(workflow, "probe_destination_writability", unexpected_probe)

    assert main(["--config", str(local_job), *command]) == 0
    assert list((local_job.parent / "destination").iterdir()) == []


@pytest.mark.parametrize("malformed", [False, True])
def test_verify_reports_stages_without_changing_verification_result(
    local_job: Path, malformed: bool
) -> None:
    if malformed:
        (local_job.parent / "destination" / ("backup-app-20260724T120000Z-" + "0" * 32)).mkdir()
    stages: list[str] = []

    if malformed:
        with pytest.raises(DestinationError, match="malformed"):
            workflow.verify_backups(local_job, progress=stages.append)
    else:
        assert workflow.verify_backups(local_job, progress=stages.append) == workflow.CommandResult(
            "verify", "verified", backups=0
        )

    assert stages == [
        "Loading and validating configuration",
        "Checking destination",
        "Verifying backup manifests, checksums, and archive contents",
    ]


@pytest.mark.parametrize("user_mode", [False, True])
def test_verify_cli_passes_mode_and_reports_verified_archives(
    local_job: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, user_mode: bool
) -> None:
    from vaultkeep.cli import commands

    archive = local_job.parent / "backup.tar.7z"

    def verify(path: Path, **kwargs: Any) -> workflow.CommandResult:
        assert path == local_job
        assert kwargs["user_mode"] is user_mode
        kwargs["on_progress"](
            VerificationProgress("archive", "Archive verification", "ok", archive)
        )
        return workflow.CommandResult("verify", "verified", backups=1)

    monkeypatch.setattr(commands, "verify_backups", verify)

    assert main([*(["--user"] if user_mode else []), "--config", str(local_job), "verify"]) == 0
    captured = capsys.readouterr()
    assert "Backup" in captured.out and "Archive" in captured.out
    assert "SUCCESS:" not in captured.err
    assert "verify: verified" not in captured.out


def test_runtime_validation_checks_marker_before_writing_probe(
    tmp_path: Path, valid_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    valid_config["destination"].update(root=str(tmp_path), marker_file="missing-marker")

    def unexpected_probe(root: Path) -> None:
        pytest.fail("Missing marker must prevent destination writes")

    monkeypatch.setattr(workflow, "probe_destination_writability", unexpected_probe)

    with pytest.raises(DestinationError, match="marker is missing"):
        workflow._validate_runtime(
            JobConfig.model_validate(valid_config),
            require_sources=False,
            require_writable_destination=True,
        )


@pytest.mark.parametrize("backup_mode", ["combined", "per_file"])
@pytest.mark.parametrize(
    "command", [["validate"], ["run"], ["list"], ["verify"], ["prune"], ["prune", "--dry-run"]]
)
def test_commands_reject_local_bind_mount_before_destination_access(
    local_job: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    backup_mode: str,
    command: list[str],
) -> None:
    data = json.loads(local_job.read_text())
    data["backup_mode"] = backup_mode
    data["destination"]["expected_fstype"] = "cifs"
    local_job.write_text(json.dumps(data))

    def unexpected_access(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("A mismatched filesystem must block destination probes and backup discovery")

    monkeypatch.setattr(
        workflow.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "ext4\n", ""),
    )
    monkeypatch.setattr(workflow, "probe_destination_writability", unexpected_access)
    monkeypatch.setattr(workflow, "discover_backups", unexpected_access)
    monkeypatch.setenv("XDG_STATE_HOME", str(local_job.parent / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(local_job.parent / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(local_job.parent / "runtime"))

    assert main(["--user", "--config", str(local_job), *command]) == 5
    assert "expected cifs, found ext4" in capsys.readouterr().err
    assert list((local_job.parent / "destination").iterdir()) == []


def test_user_workflow_paths_use_xdg_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    paths = workflow.user_workflow_paths()

    assert paths.state_root == tmp_path / "state" / "vaultkeep" / "jobs"
    assert paths.local_temp_root == tmp_path / "cache" / "vaultkeep" / "tmp"
    assert paths.lock_root == tmp_path / "runtime" / "vaultkeep" / "locks"


@pytest.mark.parametrize("user_mode", [False, True])
@pytest.mark.parametrize("action", ["install", "update", "next", "disable", "remove"])
def test_schedule_dispatches_global_job_selection(monkeypatch, tmp_path, user_mode, action):
    from vaultkeep.cli import commands

    config = tmp_path / "app.yaml"
    calls = []

    class Manager:
        def __init__(self, *, user_mode):
            calls.append(user_mode)

        def require_environment(self):
            calls.append("environment")

        def __getattr__(self, name):
            def invoke(path, *, runtime_params):
                calls.append((name, path, runtime_params))
                return "status or next run"

            return invoke

    monkeypatch.setattr(commands, "TimerManager", Manager)
    assert (
        main(
            [
                *(["--user"] if user_mode else []),
                "--config",
                str(config),
                "--param",
                "repo=nas-a",
                "schedule",
                action,
            ]
        )
        == 0
    )
    assert calls == [user_mode, "environment", (action, config, {"repo": "nas-a"})]


@pytest.mark.parametrize("action", ["list", "validate", "sync"])
def test_bulk_schedules_need_no_job_config(monkeypatch, action):
    from vaultkeep.cli import commands

    calls = []

    class Manager:
        def __init__(self, *, user_mode):
            assert user_mode

        def require_environment(self):
            pass

        def list(self):
            calls.append("list")
            return ()

        def validate_all(self):
            calls.append("validate")
            return ()

        def sync(self, *, dry_run):
            assert dry_run
            calls.append("sync")
            return ()

    monkeypatch.setattr(commands, "TimerManager", Manager)
    args = ["--user", "schedules", action]
    if action == "sync":
        args.append("--dry-run")
    assert main(args) == 0
    assert calls == [action]


@pytest.mark.parametrize("old_command", ["timer", "timers"])
def test_old_schedule_names_are_rejected(old_command):
    with pytest.raises(SystemExit) as error:
        main([old_command, "status"])
    assert error.value.code == 2


@pytest.mark.parametrize("command", ["trigger", "schedule"])
@pytest.mark.parametrize(
    "option", [["--config", "/job.yaml"], ["--param", "repo=nas-a"], ["--version"]]
)
def test_job_and_version_options_must_precede_command(command, option):
    with pytest.raises(SystemExit) as error:
        main(["--user", command, *option, "status"])
    assert error.value.code == 2


@pytest.mark.parametrize("command", ["trigger", "schedule"])
def test_shared_lifecycle_actions_provide_help(command, capsys):
    for action in ("install", "update", "disable", "remove"):
        with pytest.raises(SystemExit) as error:
            main([command, action, "--help"])
        assert error.value.code == 0
        assert f"vaultkeep {command} {action}" in capsys.readouterr().out


def test_verify_empty_destination_reports_zero_counts(local_job, capsys):
    assert main(["--config", str(local_job), "verify"]) == 0
    captured = capsys.readouterr()
    assert "Found 0 backups" in captured.err
    assert "Verified:  0/0 backups\nDuration:" in captured.out
    assert "Result:    VERIFIED\n" in captured.out
    assert "%" not in captured.err
