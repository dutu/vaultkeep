"""Conservative manager probes and read-only lock checks used by the installer."""

from __future__ import annotations

import fcntl
import importlib.util
import io
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
    "terminal,term,no_color,colored",
    [
        (True, "xterm", "", True),
        (False, "xterm", "", False),
        (True, "dumb", "", False),
        (True, "xterm", "1", False),
    ],
)
def test_helper_errors_are_explicit_and_color_only_on_terminals(
    monkeypatch, terminal: bool, term: str, no_color: str, colored: bool
) -> None:
    output = io.StringIO()
    monkeypatch.setattr(output, "isatty", lambda: terminal)
    monkeypatch.setattr(support.sys, "stderr", output)
    monkeypatch.setenv("TERM", term)
    monkeypatch.setenv("NO_COLOR", no_color)

    support.report_error("inconclusive unit properties: example.service")

    prefix = "\033[1;31mERROR:\033[0m" if colored else "ERROR:"
    assert (
        output.getvalue() == f"{prefix} installer: inconclusive unit properties: example.service\n"
    )


@pytest.mark.parametrize(
    "value",
    [None, {}, "core", [], ["trigger"], ["core", "core"], ["trigger", "core"], ["core", "unknown"]],
)
def test_components_are_explicit_and_normalized(value) -> None:
    with pytest.raises(ValueError, match="invalid components"):
        support.selection(value)


@pytest.mark.parametrize("returncode", [0, 1])
@pytest.mark.parametrize("empty_fields", [False, True])
def test_absent_unit_accepts_missing_service_properties(
    monkeypatch, returncode: int, empty_fields: bool
) -> None:
    # systemd 257 does not expose ExecStart for an absent worker instance, even
    # with --all. Empty properties may also be omitted from the response.
    output = "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
    if empty_fields:
        output += "FragmentPath=\nDropInPaths=\nUnitFileState=\n"

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, output, "")

    monkeypatch.setattr(support.subprocess, "run", run)
    state = support.properties(["systemctl"], "vaultkeep-trigger-exec@installer-check.service")
    assert state["LoadState"] == "not-found"
    assert state["ExecStart"] == ""


@pytest.mark.parametrize(
    "output,returncode",
    [
        ("", 1),
        ("LoadState=not-found\nActiveState=inactive\n", 1),
        ("LoadState=not-found\nActiveState=inactive\nSubState=dead\n", 2),
        ("LoadState=not-found\nActiveState=active\nSubState=running\n", 1),
        (
            "LoadState=loaded\nActiveState=inactive\nSubState=dead\n"
            "FragmentPath=/etc/systemd/user/vaultkeep-trigger.service\nDropInPaths=\n"
            "UnitFileState=disabled\nExecStart=/usr/local/bin/vaultkeep --user trigger monitor\n",
            1,
        ),
    ],
)
def test_inconclusive_absence_or_failed_query_is_refused(monkeypatch, output, returncode) -> None:
    monkeypatch.setattr(
        support.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, returncode, output, "bus unavailable"),
    )
    with pytest.raises(ValueError, match="inconclusive"):
        support.properties(["systemctl"], "vaultkeep-trigger-exec@installer-check.service")


@pytest.mark.parametrize("unit", ["vaultkeep@demo.timer", "vaultkeep-trigger.service"])
def test_loaded_unit_requires_only_applicable_properties(monkeypatch, unit: str) -> None:
    output = (
        "LoadState=loaded\nActiveState=inactive\nSubState=dead\n"
        "FragmentPath=/etc/systemd/system/vaultkeep@.timer\nDropInPaths=\n"
        "UnitFileState=disabled\n"
    )
    monkeypatch.setattr(
        support.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, output, ""),
    )
    if unit.endswith(".timer"):
        assert support.properties(["systemctl"], unit)["LoadState"] == "loaded"
    else:
        with pytest.raises(ValueError, match="inconclusive"):
            support.properties(["systemctl"], unit)


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
        (
            "/usr/local/bin/vaultkeep --user trigger monitor",
            "vaultkeep-trigger@job-identity.service",
            False,
        ),
        (
            "/usr/local/bin/vaultkeep --user trigger --config /private/config.toml monitor",
            "vaultkeep-trigger@job-identity.service",
            True,
        ),
        (
            "/usr/local/bin/vaultkeep --user trigger --config relative.toml monitor",
            "vaultkeep-trigger.service",
            False,
        ),
        (
            "/usr/local/bin/vaultkeep --user trigger exec-worker %i",
            "vaultkeep-trigger-exec@.service",
            True,
        ),
        (
            "/usr/local/bin/vaultkeep --user trigger exec-worker 123",
            "vaultkeep-trigger-exec@123.service",
            True,
        ),
        (
            "/usr/local/bin/vaultkeep --user trigger --config /config exec-worker %i",
            "vaultkeep-trigger-exec@.service",
            False,
        ),
        ("/old/vaultkeep-trigger monitor", "vaultkeep-trigger.service", False),
    ],
)
def test_effective_trigger_commands_preserve_config_and_worker_boundary(argv, unit, valid) -> None:
    executable = Path("/usr/local/bin/vaultkeep")
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
        trigger_bin=Path("/usr/local/bin/vaultkeep"),
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
                trigger_bin=Path("/usr/local/bin/vaultkeep"),
                validate_trigger=True,
            )
    else:
        support.check_manager(
            ["systemctl", "--user"],
            "alice",
            uninstall=False,
            trigger_bin=Path("/usr/local/bin/vaultkeep"),
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
                    "{ path=/usr/local/bin/vaultkeep ; "
                    "argv[]=/usr/local/bin/vaultkeep --user trigger exec-worker installer-check ; "
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
            trigger_bin=Path("/usr/local/bin/vaultkeep"),
        )


@pytest.mark.parametrize("issue", [None, "unmanaged", "override"])
def test_per_job_monitors_are_checked_without_changing_user_units(tmp_path, monkeypatch, issue):
    unit = "vaultkeep-trigger@app-0123456789abcdef.service"
    fragment = tmp_path / unit
    content = f"# Managed by Vaultkeep: {unit}\n[Service]\n"
    fragment.write_text(content if issue != "unmanaged" else "# operator-owned\n")
    before = fragment.read_bytes()

    def execute(argv):
        return f"{unit} loaded inactive dead monitor\n" if "list-units" in argv else ""

    monkeypatch.setattr(support, "execute", execute)

    def properties(_argv, name):
        if name != unit:
            return {
                "LoadState": "not-found",
                "ActiveState": "inactive",
                "SubState": "dead",
                "FragmentPath": "",
                "DropInPaths": "",
                "ExecStart": "",
                "UnitFileState": "",
            }
        return {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "SubState": "dead",
            "FragmentPath": str(fragment),
            "DropInPaths": "/override.conf" if issue == "override" else "",
            "ExecStart": (
                "{ path=/usr/local/bin/vaultkeep ; "
                "argv[]=/usr/local/bin/vaultkeep --user trigger --config /jobs/app.yaml monitor ; "
                "ignore_errors=no ; }"
            ),
            "UnitFileState": "enabled",
            "Type": "simple",
            "Restart": "on-failure",
            "UMask": "0077",
        }

    monkeypatch.setattr(support, "properties", properties)
    if issue:
        with pytest.raises(ValueError, match="review"):
            support.check_manager(
                ["systemctl", "--user"],
                "alice",
                uninstall=False,
                trigger_bin=Path("/usr/local/bin/vaultkeep"),
            )
    else:
        support.check_manager(
            ["systemctl", "--user"],
            "alice",
            uninstall=False,
            trigger_bin=Path("/usr/local/bin/vaultkeep"),
        )
    assert fragment.read_bytes() == before
