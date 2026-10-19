"""Conservative manager probes and read-only lock checks used by the installer."""

from __future__ import annotations

import fcntl
import importlib.util
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "install_support", Path(__file__).resolve().parents[2] / "scripts/install_support.py"
)
assert SPEC is not None and SPEC.loader is not None
support = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(support)


@pytest.mark.parametrize(
    "value",
    [None, {}, "core", [], ["trigger"], ["core", "core"], ["trigger", "core"], ["core", "unknown"]],
)
def test_components_are_explicit_and_normalized(value) -> None:
    with pytest.raises(ValueError, match="invalid components"):
        support.selection(value)


def test_absent_unit_nonzero_requires_complete_affirmative_properties(monkeypatch) -> None:
    output = (
        "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
        "FragmentPath=\nDropInPaths=\nExecStart=\nUnitFileState=not-found\n"
    )
    monkeypatch.setattr(
        support.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, output, "")
    )
    assert (
        support.properties(["systemctl"], "vaultkeep-trigger.service")["LoadState"] == "not-found"
    )
    monkeypatch.setattr(
        support.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "bus unavailable"),
    )
    with pytest.raises(ValueError, match="inconclusive"):
        support.properties(["systemctl"], "vaultkeep-trigger.service")


def test_lock_check_does_not_create_or_change_evidence(tmp_path: Path) -> None:
    path = tmp_path / "monitor.lock"
    support.check_lock(path, "test-account")
    assert not path.exists()
    path.write_bytes(b"unchanged owner evidence\n")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    with path.open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="held runtime/job lock"):
            support.check_lock(path, "test-account")
    support.check_lock(path, "test-account")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_lock_symlink_is_refused_without_following(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"evidence")
    link = tmp_path / "monitor.lock"
    link.symlink_to(target)
    with pytest.raises(OSError):
        support.check_lock(link, "test-account")
    assert target.read_bytes() == b"evidence"


@pytest.mark.parametrize(
    "argv,unit,valid",
    [
        ("/usr/local/bin/vaultkeep-trigger monitor", "vaultkeep-trigger.service", True),
        (
            "/usr/local/bin/vaultkeep-trigger --config /private/config.toml monitor",
            "vaultkeep-trigger.service",
            True,
        ),
        (
            "/usr/local/bin/vaultkeep-trigger --config relative.toml monitor",
            "vaultkeep-trigger.service",
            False,
        ),
        (
            "/usr/local/bin/vaultkeep-trigger exec-worker %i",
            "vaultkeep-trigger-exec@.service",
            True,
        ),
        (
            "/usr/local/bin/vaultkeep-trigger exec-worker 123",
            "vaultkeep-trigger-exec@123.service",
            True,
        ),
        (
            "/usr/local/bin/vaultkeep-trigger --config /config exec-worker %i",
            "vaultkeep-trigger-exec@.service",
            False,
        ),
        ("/old/vaultkeep-trigger monitor", "vaultkeep-trigger.service", False),
    ],
)
def test_effective_trigger_commands_preserve_config_and_worker_boundary(argv, unit, valid) -> None:
    executable = Path("/usr/local/bin/vaultkeep-trigger")
    state = {"ExecStart": f"{{ path={argv.split()[0]} ; argv[]={argv} ; ignore_errors=no ; }}"}
    if valid:
        support.check_trigger_command(state, unit, executable, "test-account")
    else:
        with pytest.raises(ValueError, match="ExecStart"):
            support.check_trigger_command(state, unit, executable, "test-account")


def test_manager_probe_checks_queued_jobs_and_complete_enablement(monkeypatch) -> None:
    calls = []

    def execute(argv):
        calls.append(argv)
        if "list-jobs" in argv:
            return "3 vaultkeep-trigger-exec@abc.service start waiting\n"
        return ""

    monkeypatch.setattr(support, "execute", execute)
    with pytest.raises(ValueError, match="queued job"):
        support.check_manager(
            ["systemctl"], "test", uninstall=False, trigger_bin=Path("/bin/trigger")
        )
    assert any("list-units" in c for c in calls)
    assert any("list-jobs" in c for c in calls)


def test_no_running_user_manager_is_valid(monkeypatch) -> None:
    monkeypatch.setattr(support, "execute", lambda argv: "")
    assert support.user_managers() == []


def test_transitioning_manager_is_not_ignored(monkeypatch) -> None:
    monkeypatch.setattr(
        support, "execute", lambda argv: "user@1001.service loaded deactivating stop user\n"
    )
    with pytest.raises(ValueError, match="transitioning user manager"):
        support.user_managers()


def test_user_reload_attempts_every_manager_and_reports_each_failure(monkeypatch) -> None:
    monkeypatch.setattr(support, "user_managers", lambda: [("alice", 1001), ("bob", 1002)])
    commands = []

    def execute(argv):
        commands.append(argv)
        raise ValueError("reload failed")

    monkeypatch.setattr(support, "execute", execute)
    monkeypatch.setattr(support.sys, "argv", ["helper", "reload-users"])
    with pytest.raises(ValueError, match=r"alice:.*bob:"):
        support.main()
    assert len(commands) == 2


@pytest.mark.parametrize("uninstall", [False, True])
def test_user_manager_with_no_trigger_template_is_valid(monkeypatch, uninstall: bool) -> None:
    calls = []
    monkeypatch.setattr(support, "execute", lambda argv: calls.append(argv) or "")

    def properties(argv, unit):
        assert "@.service" not in unit
        return {
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "SubState": "dead",
            "FragmentPath": "",
            "DropInPaths": "",
            "ExecStart": "",
            "UnitFileState": "",
        }

    monkeypatch.setattr(support, "properties", properties)
    support.check_manager(
        ["systemctl", "--user"],
        "alice",
        uninstall=uninstall,
        trigger_bin=Path("/usr/local/bin/vaultkeep-trigger"),
    )
    assert any("list-jobs" in call for call in calls)


@pytest.mark.parametrize("validate_trigger", [False, True])
def test_paused_standalone_monitor_only_conflicts_when_selecting_trigger(
    monkeypatch, validate_trigger
):
    monkeypatch.setattr(support, "execute", lambda argv: "")

    def properties(argv, unit):
        state = {
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "SubState": "dead",
            "FragmentPath": "",
            "DropInPaths": "",
            "ExecStart": "",
            "UnitFileState": "",
        }
        if unit == "vaultkeep-trigger.service":
            state.update(
                LoadState="loaded",
                FragmentPath="/home/alice/.config/systemd/user/vaultkeep-trigger.service",
                ExecStart="old monitor",
                UnitFileState="enabled",
            )
        return state

    monkeypatch.setattr(support, "properties", properties)
    if validate_trigger:
        with pytest.raises(ValueError, match="migrate/review"):
            support.check_manager(
                ["systemctl", "--user"],
                "alice",
                uninstall=False,
                trigger_bin=Path("/usr/local/bin/vaultkeep-trigger"),
                validate_trigger=True,
            )
    else:
        support.check_manager(
            ["systemctl", "--user"],
            "alice",
            uninstall=False,
            trigger_bin=Path("/usr/local/bin/vaultkeep-trigger"),
            validate_trigger=False,
        )


def test_worker_lifecycle_override_is_a_conflict(monkeypatch):
    monkeypatch.setattr(support, "execute", lambda argv: "")

    def properties(argv, unit):
        state = {
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "SubState": "dead",
            "FragmentPath": "",
            "DropInPaths": "",
            "ExecStart": "",
            "UnitFileState": "",
        }
        if unit.startswith("vaultkeep-trigger-exec@"):
            state.update(
                LoadState="loaded",
                FragmentPath="/etc/systemd/user/vaultkeep-trigger-exec@.service",
                ExecStart=(
                    "{ path=/usr/local/bin/vaultkeep-trigger ; "
                    "argv[]=/usr/local/bin/vaultkeep-trigger exec-worker installer-check ; "
                    "ignore_errors=no ; }"
                ),
                Type="oneshot",
                Restart="no",
                UMask="0077",
                TimeoutStartUSec="infinity",
                PartOf="vaultkeep-trigger.service",
                BindsTo="",
            )
        return state

    monkeypatch.setattr(support, "properties", properties)
    with pytest.raises(ValueError, match="lifecycle override"):
        support.check_manager(
            ["systemctl", "--user"],
            "alice",
            uninstall=False,
            trigger_bin=Path("/usr/local/bin/vaultkeep-trigger"),
        )
