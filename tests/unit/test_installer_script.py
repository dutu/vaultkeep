"""Execute installer transactions against temporary roots and controlled host tools."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "install.sh"
CANDIDATE_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


@pytest.fixture
def host(tmp_path: Path) -> dict[str, str]:
    """Fake service managers/ownership; use real Python, pip, rsync and filesystem."""
    fake = tmp_path / "host-bin"
    fake.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "VAULTKEEP_INSTALL_TESTING": "1",
            "VAULTKEEP_INSTALL_PREFIX": str(tmp_path / "opt/vaultkeep"),
            "VAULTKEEP_CONFIG_ROOT": str(tmp_path / "etc/vaultkeep"),
            "VAULTKEEP_VAR_ROOT": str(tmp_path / "var/lib/vaultkeep"),
            "VAULTKEEP_BIN_LINK": str(tmp_path / "usr/local/bin/vaultkeep"),
            "VAULTKEEP_TRIGGER_BIN_LINK": str(tmp_path / "usr/local/bin/vaultkeep-trigger"),
            "VAULTKEEP_SYSTEMD_ROOT": str(tmp_path / "etc/systemd/system"),
            "VAULTKEEP_USER_SYSTEMD_ROOT": str(tmp_path / "etc/systemd/user"),
            "PATH": f"{fake}:/usr/bin:/bin",
            "HOST_LOG": str(tmp_path / "host.log"),
            "HOST_CONTROL": str(tmp_path / "control.json"),
            "PIP_NO_INDEX": "1",
            "PIP_FIND_LINKS": os.environ.get(
                "VAULTKEEP_TEST_WHEELHOUSE", "/tmp/vaultkeep-wheelhouse"
            ),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    Path(environment["HOST_CONTROL"]).write_text("{}")
    os_release = tmp_path / "os-release"
    os_release.write_text("ID=debian\nVERSION_ID=13\nVERSION_CODENAME=trixie\n")
    environment["VAULTKEEP_TEST_OS_RELEASE"] = str(os_release)
    # Production code still executes all checks; these replace external host actions.
    executable(fake / "python3", f"#!/bin/sh\nexec '{sys.executable}' \"$@\"\n")
    executable(fake / "chown", "#!/bin/sh\nexit 0\n")
    executable(
        fake / "install",
        """#!/usr/bin/python3
import os, sys
args = sys.argv[1:]
for option in ('-o', '-g'):
    while option in args:
        index = args.index(option)
        del args[index:index + 2]
os.execv('/usr/bin/install', ['install', *args])
""",
    )
    executable(fake / "systemd-analyze", "#!/bin/sh\nexit 0\n")
    executable(fake / "zstd", "#!/bin/sh\nexit 0\n")
    executable(
        fake / "systemctl",
        """#!/usr/bin/python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['HOST_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args and args[0] == '--user':
    args = args[1:]
control = json.loads(Path(os.environ['HOST_CONTROL']).read_text())
if any(arg in args for arg in ('start', 'stop', 'enable', 'disable', 'preset')):
    raise SystemExit('installer attempted service activation or enablement change')
if args[0] == 'list-units':
    if 'user@*.service' in args:
        print(control.get('managers', ''), end='')
    else:
        print(control.get('units', ''), end='')
elif args[0] == 'list-jobs':
    print(control.get('jobs', ''), end='')
elif args[0] == 'list-unit-files':
    print(control.get('unit_files', ''), end='')
elif args[0] == 'show':
    print(control.get('properties', 'LoadState=loaded\\nActiveState=active\\nSubState=waiting'))
elif args[0] == 'daemon-reload':
    if control.get('reload_fail_once'):
        control['reload_fail_once'] = False
        Path(os.environ['HOST_CONTROL']).write_text(json.dumps(control))
        raise SystemExit(1)
    if control.get('signal'):
        os.kill(os.getppid(), int(control['signal']))
        control.pop('signal')
        Path(os.environ['HOST_CONTROL']).write_text(json.dumps(control))
else:
    raise SystemExit('unexpected systemctl call: ' + repr(args))
""",
    )
    return environment


@pytest.fixture(scope="module", autouse=True)
def frozen_source(tmp_path_factory: pytest.TempPathFactory):
    # Tests keep one immutable candidate while docs or unrelated work may change.
    source = tmp_path_factory.mktemp("installer-source")
    subprocess.run(
        [
            "rsync",
            "-a",
            "--exclude=.git/",
            "--exclude=.venv/",
            "--exclude=__pycache__/",
            "--exclude=dist/",
            "--exclude=.aws/",
            "--exclude=.codex/",
            "--exclude=.agents/",
            f"{ROOT}/",
            str(source),
        ],
        check=True,
    )
    global CANDIDATE_SOURCE
    CANDIDATE_SOURCE = source
    return source


def run_installer(
    host: dict[str, str], *arguments: str, check: bool = True, source: Path | None = None
) -> subprocess.CompletedProcess[str]:
    source = source or CANDIDATE_SOURCE
    result = subprocess.run(
        ["bash", str(source / "install.sh"), *arguments],
        env=host,
        cwd=source,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def manifest(host: dict[str, str]) -> dict:
    return json.loads(
        (Path(host["VAULTKEEP_INSTALL_PREFIX"]) / "install-manifest.json").read_text()
    )


def control(host: dict[str, str], **settings: object) -> None:
    Path(host["HOST_CONTROL"]).write_text(json.dumps(settings))


def wheelhouse_available(host: dict[str, str]) -> None:
    if not list(Path(host["PIP_FIND_LINKS"]).glob("hatchling-*.whl")):
        pytest.fail("prepare the locked binary wheelhouse; see docs/development/testing.md")


@pytest.mark.parametrize(
    "arguments,status,message",
    [
        (["invalid"], 2, "unknown mode: invalid"),
        (["install", "--unknown"], 2, "unknown option: --unknown"),
        (["install", "--with-trigger"], 2, "unknown option: --with-trigger"),
    ],
)
def test_installer_errors_have_explicit_prefix_in_plain_logs(
    host: dict[str, str], arguments: list[str], status: int, message: str
) -> None:
    host["TERM"] = "xterm"
    result = run_installer(host, *arguments, check=False)
    assert result.returncode == status
    assert result.stderr.startswith(f"ERROR: install.sh: {message}\n")
    assert "\033[" not in result.stderr


def test_helper_cli_errors_have_explicit_prefix_in_plain_logs(host: dict[str, str]) -> None:
    result = subprocess.run(
        [sys.executable, str(CANDIDATE_SOURCE / "scripts/install_support.py"), "unsupported"],
        env=host,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "ERROR: installer: unknown installer check: unsupported\n"


@pytest.mark.parametrize(
    "term,no_color,colored",
    [("xterm", "", True), ("dumb", "", False), ("xterm", "1", False)],
)
def test_installer_error_prefix_on_a_terminal(
    host: dict[str, str], term: str, no_color: str, colored: bool
) -> None:
    import pty

    host.update(TERM=term, NO_COLOR=no_color)
    master, slave = pty.openpty()
    try:
        result = subprocess.run(
            ["bash", str(INSTALLER), "invalid"],
            env=host,
            stdout=subprocess.PIPE,
            stderr=slave,
            timeout=10,
            check=False,
        )
    finally:
        os.close(slave)
    output = b""
    try:
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                break
            if not chunk:
                break
            output += chunk
    finally:
        os.close(master)
    assert result.returncode == 2
    prefix = "\033[1;31mERROR:\033[0m" if colored else "ERROR:"
    assert output.decode().startswith(f"{prefix} install.sh: unknown mode: invalid")
    if not colored:
        assert b"\033[" not in output


@pytest.fixture
def installed(host: dict[str, str]) -> dict[str, str]:
    wheelhouse_available(host)
    run_installer(host, "install")
    return host


def snapshot(host: dict[str, str]) -> dict[str, tuple[str, bytes | str]]:
    result = {}
    for name in (
        "VAULTKEEP_INSTALL_PREFIX",
        "VAULTKEEP_SYSTEMD_ROOT",
        "VAULTKEEP_USER_SYSTEMD_ROOT",
        "VAULTKEEP_CONFIG_ROOT",
        "VAULTKEEP_VAR_ROOT",
    ):
        base = Path(host[name])
        for path in base.rglob("*"):
            if path.is_symlink():
                result[str(path)] = ("symlink", os.readlink(path))
            elif path.is_file() and not path.name.endswith(".pyc"):
                result[str(path)] = ("file", path.read_bytes())
    for name in ("VAULTKEEP_BIN_LINK", "VAULTKEEP_TRIGGER_BIN_LINK"):
        path = Path(host[name])
        if path.is_symlink():
            result[str(path)] = ("symlink", os.readlink(path))
    return result


def legacy_fixture(host: dict[str, str], version: str = "0.1.0") -> dict:
    """Actual schema-1 field set and records emitted by the 0.1.0 installer."""
    prefix = Path(host["VAULTKEEP_INSTALL_PREFIX"])
    release = prefix / "releases" / version
    release.mkdir(parents=True)
    current = prefix / "current"
    current.symlink_to(f"releases/{version}")
    binary = Path(host["VAULTKEEP_BIN_LINK"])
    binary.parent.mkdir(parents=True)
    binary.symlink_to(current / "venv/bin/vaultkeep")
    paths = [prefix, release, current, binary]
    for name in ("vaultkeep@.service", "vaultkeep@.timer"):
        destination = Path(host["VAULTKEEP_SYSTEMD_ROOT"]) / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "systemd" / name, destination)
        paths.append(destination)
    registry = Path(host["VAULTKEEP_VAR_ROOT"]) / "systemd-instances.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("{}\n")
    paths.append(registry)
    records = []
    for path in paths:
        if path.is_symlink():
            records.append({"path": str(path), "type": "symlink", "target": os.readlink(path)})
        else:
            records.append(
                {
                    "path": str(path),
                    "type": "file" if path.is_file() else "directory",
                    "mode": oct(path.stat().st_mode & 0o777),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
                    if path.is_file()
                    else "",
                }
            )
    data = {
        "schema_version": 1,
        "version": version,
        "source_digest": "a" * 64,
        "prefix": str(prefix),
        "active_release": str(release),
        "retained_release": "",
        "current_symlink": str(current),
        "bin_symlink": str(binary),
        "config_root": host["VAULTKEEP_CONFIG_ROOT"],
        "var_root": host["VAULTKEEP_VAR_ROOT"],
        "timer_registry": str(registry),
        "owned_artifacts": records,
    }
    (prefix / "install-manifest.json").write_text(json.dumps(data))
    (release / "deployment.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": version,
                "source_digest": "a" * 64,
                "source_checkout": str(ROOT),
            }
        )
    )
    return data


def test_bash_syntax() -> None:
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)


def test_dry_run_is_read_only(host: dict[str, str]) -> None:
    before = snapshot(host)
    result = run_installer(host, "install", "--dry-run")
    assert "Trigger is included; monitoring remains opt-in" in result.stdout
    assert "PLAN stage release" in result.stdout
    assert "activity checks are not a live quiescence guarantee" in result.stdout
    assert snapshot(host) == before
    assert not Path(host["HOST_LOG"]).exists()
    assert not Path(host["VAULTKEEP_INSTALL_PREFIX"]).exists()


@pytest.mark.parametrize("mode", ["install", "update"])
def test_same_version_source_guard(host: dict[str, str], mode: str) -> None:
    legacy_fixture(host, CANDIDATE_VERSION)
    result = run_installer(host, mode, "--dry-run", check=False)
    if mode == "update":
        assert result.returncode != 0
        assert "same version with different source digest" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "reinstalling from current checkout" in result.stdout


def test_update_needs_manifest(host: dict[str, str]) -> None:
    result = run_installer(host, "update", "--dry-run", check=False)
    assert result.returncode != 0
    assert "update requires an existing installation manifest" in result.stderr


def test_fresh_install_packages_and_relocated_scripts(host: dict[str, str]) -> None:
    wheelhouse_available(host)
    run_installer(host, "install")
    data = manifest(host)
    assert data["schema_version"] == 3
    assert "components" not in data
    release = Path(data["active_release"])
    assert release.name == CANDIDATE_VERSION
    assert "components" not in json.loads((release / "deployment.json").read_text())
    for prefix in ([], ["trigger"]):
        assert (
            subprocess.check_output(
                [host["VAULTKEEP_BIN_LINK"], *prefix, "--version"], text=True
            ).strip()
            == CANDIDATE_VERSION
        )
    worker = Path(host["VAULTKEEP_USER_SYSTEMD_ROOT"]) / "vaultkeep-trigger-exec@.service"
    assert "TimeoutStartSec=infinity" in worker.read_text()
    assert "PartOf=" not in worker.read_text()
    assert "vaultkeep --user trigger exec-worker %i" in worker.read_text()
    assert not Path(host["VAULTKEEP_TRIGGER_BIN_LINK"]).exists()
    first = snapshot(host)
    run_installer(host, "update")
    assert snapshot(host) == first


def test_refresh_with_running_user_manager_and_absent_units(
    installed: dict[str, str], tmp_path: Path
) -> None:
    source = tmp_path / "candidate-source"
    shutil.copytree(CANDIDATE_SOURCE, source)
    runtime = tmp_path / "user-runtime"
    runtime.mkdir(mode=0o700)
    account_home = tmp_path / "user-home"
    account_home.mkdir()
    installed["HOST_USER_RUNTIME"] = str(runtime)
    installed["HOST_USER_HOME"] = str(account_home)
    helper = source / "scripts/install_support.py"
    helper.write_text(
        helper.read_text()
        .replace('Path(f"/run/user/{uid}")', 'Path(os.environ["HOST_USER_RUNTIME"])')
        .replace("Path(pwd.getpwuid(uid).pw_dir)", 'Path(os.environ["HOST_USER_HOME"])')
    )
    fake = Path(installed["PATH"].split(":")[0])
    executable(
        fake / "runuser",
        """#!/usr/bin/python3
import os, sys
args = sys.argv[sys.argv.index('systemctl'):]
os.execvp('systemctl', args)
""",
    )
    control(
        installed,
        managers=f"user@{os.getuid()}.service loaded active running user manager\n",
        properties="LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
        "FragmentPath=\nDropInPaths=\nUnitFileState=\n",
    )
    previous = manifest(installed)

    run_installer(installed, "install", source=source)

    assert "components" not in manifest(installed)
    assert manifest(installed)["retained_release"] == previous["retained_release"]
    assert (
        subprocess.check_output(
            [installed["VAULTKEEP_BIN_LINK"], "trigger", "--version"], text=True
        ).strip()
        == CANDIDATE_VERSION
    )
    calls = [json.loads(line) for line in Path(installed["HOST_LOG"]).read_text().splitlines()]
    assert any(
        "--user" in call and "vaultkeep-trigger-exec@installer-check.service" in call
        for call in calls
    )
    assert ["--user", "daemon-reload"] in calls


def test_legacy_upgrade_preserves_previous_and_data(host: dict[str, str]) -> None:
    wheelhouse_available(host)
    old = legacy_fixture(host)
    secrets = Path(host["VAULTKEEP_CONFIG_ROOT"]) / "secrets"
    secrets.mkdir(parents=True)
    marker = secrets / "test.password"
    marker.write_bytes(b"preserved secret\n")
    run_installer(host, "update")
    assert manifest(host)["retained_release"] == old["active_release"]
    assert marker.read_bytes() == b"preserved secret\n"
    assert Path(old["active_release"]).is_dir()


@pytest.mark.parametrize(
    "components", [[], ["trigger"], ["core", "core"], ["core", "unknown"], "core", None]
)
def test_invalid_components_never_fall_back_to_core(
    host: dict[str, str], components: object
) -> None:
    data = legacy_fixture(host)
    data.update(schema_version=2, components=components)
    prefix = Path(host["VAULTKEEP_INSTALL_PREFIX"])
    (prefix / "install-manifest.json").write_text(json.dumps(data))
    before = snapshot(host)
    result = run_installer(host, "update", check=False)
    assert result.returncode != 0
    assert "invalid components" in result.stderr
    assert snapshot(host) == before
    assert not Path(host["HOST_LOG"]).exists()


@pytest.mark.parametrize("kind", ["unit", "bin", "drop-in", "current"])
def test_ownership_conflicts_refuse_before_staging(installed: dict[str, str], kind: str) -> None:
    if kind == "unit":
        unit = Path(installed["VAULTKEEP_SYSTEMD_ROOT"]) / "vaultkeep@.service"
        unit.write_text("# operator modified\n")
    elif kind == "drop-in":
        path = Path(installed["VAULTKEEP_SYSTEMD_ROOT"]) / "vaultkeep@.service.d"
        path.mkdir()
        (path / "override.conf").write_text("[Service]\n")
    else:
        path = (
            Path(installed["VAULTKEEP_BIN_LINK"])
            if kind == "bin"
            else Path(installed["VAULTKEEP_INSTALL_PREFIX"]) / "current"
        )
        path.unlink()
        path.symlink_to("/unrelated")
    before = snapshot(installed)
    result = run_installer(installed, "update", check=False)
    assert result.returncode != 0
    assert snapshot(installed) == before


@pytest.mark.parametrize(
    "damage",
    [
        "missing-package",
        "entry-point",
        "missing-trigger-dependency",
        "missing-unit",
        "missing-link",
    ],
)
def test_exact_match_repairs_environment_or_missing_artifacts(
    installed: dict[str, str], damage: str
) -> None:
    data = manifest(installed)
    release = Path(data["active_release"])
    if damage == "missing-package":
        subprocess.run(
            [str(release / "venv/bin/python"), "-m", "pip", "uninstall", "-y", "vaultkeep"],
            check=True,
            capture_output=True,
        )
    elif damage == "entry-point":
        (release / "venv/bin/vaultkeep").write_text("#!/nonexistent/python\n")
    elif damage == "missing-trigger-dependency":
        subprocess.run(
            [str(release / "venv/bin/python"), "-m", "pip", "uninstall", "-y", "psutil"],
            check=True,
            capture_output=True,
        )
    elif damage == "missing-unit":
        (Path(installed["VAULTKEEP_SYSTEMD_ROOT"]) / "vaultkeep@.service").unlink()
    else:
        Path(installed["VAULTKEEP_BIN_LINK"]).unlink()
    run_installer(installed, "update")
    assert manifest(installed) == data
    assert (
        subprocess.check_output([installed["VAULTKEEP_BIN_LINK"], "--version"], text=True).strip()
        == CANDIDATE_VERSION
    )


@pytest.mark.parametrize(
    "setting",
    [
        {"units": "vaultkeep@demo.timer loaded active waiting demo\n"},
        {"units": "vaultkeep-trigger-exec@abc.service loaded activating start demo\n"},
        {"jobs": "4 vaultkeep@demo.service start waiting\n"},
        {"managers": "user@1001.service loaded activating start user\n"},
    ],
)
def test_active_or_ambiguous_work_refuses_without_mutation(
    installed: dict[str, str], setting: dict
) -> None:
    control(installed, **setting)
    before = snapshot(installed)
    result = run_installer(installed, "update", check=False)
    assert result.returncode != 0
    assert snapshot(installed) == before


@pytest.mark.parametrize("fresh", [False, True])
def test_reload_failure_rolls_back_refresh_or_fresh_install(
    host: dict[str, str], fresh: bool
) -> None:
    wheelhouse_available(host)
    if not fresh:
        run_installer(host, "install")
    if not fresh:
        (Path(host["VAULTKEEP_USER_SYSTEMD_ROOT"]) / "vaultkeep-trigger-exec@.service").unlink()
    before = snapshot(host)
    control(host, reload_fail_once=True)
    result = run_installer(host, "install", check=False)
    assert result.returncode != 0
    assert "ERROR: install.sh: installer transaction failed (exit 1);" in result.stderr
    after = snapshot(host)
    # The inactive packaged example and empty install lock may remain on first failure.
    ignored = ("install.lock", ".example")
    assert {k: v for k, v in after.items() if not k.endswith(ignored)} == {
        k: v for k, v in before.items() if not k.endswith(ignored)
    }
    assert not Path(host["VAULTKEEP_TRIGGER_BIN_LINK"]).exists()
    if not fresh:
        assert "components" not in manifest(host)
        assert (
            subprocess.check_output([host["VAULTKEEP_BIN_LINK"], "--version"], text=True).strip()
            == CANDIDATE_VERSION
        )


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_handled_signal_rolls_back_at_reload(installed: dict[str, str], signum: int) -> None:
    (Path(installed["VAULTKEEP_USER_SYSTEMD_ROOT"]) / "vaultkeep-trigger-exec@.service").unlink()
    before = snapshot(installed)
    control(installed, signal=signum)
    result = run_installer(installed, "install", check=False)
    assert result.returncode != 0
    assert snapshot(installed) == before


@pytest.mark.parametrize("purge", [False, True])
def test_uninstall_preserves_user_evidence(host: dict[str, str], purge: bool) -> None:
    wheelhouse_available(host)
    run_installer(host, "install")
    evidence = (
        Path(host["HOST_LOG"]).parent
        / "home/user/.local/state/vaultkeep-trigger/attempts/receipt.json"
    )
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b'{"schema_version":1,"outcome":"review_required"}\n')
    worker = Path(host["VAULTKEEP_USER_SYSTEMD_ROOT"]) / "vaultkeep-trigger-exec@.service"
    assert worker.is_file()
    monitor = (
        evidence.parents[4] / ".config/systemd/user/vaultkeep-trigger@app-0123456789abcdef.service"
    )
    monitor.parent.mkdir(parents=True)
    monitor.write_bytes(b"# user-owned per-job monitor\n[Service]\n")
    monitor_before = monitor.read_bytes()
    before = evidence.read_bytes()
    result = run_installer(host, "uninstall", "--dry-run", *(["--purge"] if purge else []))
    assert "preserve every user's Trigger" in result.stdout
    run_installer(host, "uninstall", *(["--purge"] if purge else []))
    assert evidence.read_bytes() == before
    assert not worker.exists()
    assert monitor.read_bytes() == monitor_before
    assert not Path(host["VAULTKEEP_INSTALL_PREFIX"]).exists()
    assert not Path(host["VAULTKEEP_TRIGGER_BIN_LINK"]).exists()
    assert Path(host["VAULTKEEP_CONFIG_ROOT"]).exists() != purge


@pytest.mark.parametrize("mode", ["install", "update"])
def test_debian12_rejected_before_mutation(host: dict[str, str], mode: str) -> None:
    os_release = Path(host["HOST_CONTROL"]).with_suffix(".os-release")
    os_release.write_text("ID=debian\nVERSION_ID=12\nVERSION_CODENAME=bookworm\n")
    host["VAULTKEEP_TEST_OS_RELEASE"] = str(os_release)
    result = run_installer(host, mode, check=False)
    assert result.returncode != 0
    assert "requires Debian 13" in result.stderr
    assert not Path(host["VAULTKEEP_INSTALL_PREFIX"]).exists()
    assert not Path(host["HOST_LOG"]).exists()


@pytest.mark.parametrize("signum", [0, signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_staging_failure_or_signal_removes_candidate(host: dict[str, str], signum: int) -> None:
    fake = Path(host["PATH"].split(":")[0])
    executable(
        fake / "python3",
        f"""#!/usr/bin/python3
import os, sys
if sys.argv[1:3] == ['-m', 'venv']:
    if {int(signum)}:
        os.kill(os.getppid(), {int(signum)})
    raise SystemExit(1)
os.execv({sys.executable!r}, ['python3', *sys.argv[1:]])
""",
    )
    result = run_installer(host, "install", check=False)
    assert result.returncode != 0
    prefix = Path(host["VAULTKEEP_INSTALL_PREFIX"])
    assert not list((prefix / ".staging").iterdir())
    assert not list((prefix / "releases").iterdir())
    assert not (prefix / "install-manifest.json").exists()
    assert not Path(host["VAULTKEEP_TRIGGER_BIN_LINK"]).exists()


def test_existing_older_python_rejected_before_lock_or_host_actions(host: dict[str, str]) -> None:
    fake = Path(host["PATH"].split(":")[0])
    executable(fake / "python3", "#!/bin/sh\nexit 1\n")
    result = run_installer(host, "install", check=False)
    assert result.returncode != 0
    assert "python3 >=3.13" in result.stderr
    assert not Path(host["VAULTKEEP_INSTALL_PREFIX"]).exists()
    assert not Path(host["HOST_LOG"]).exists()


def test_same_version_refresh_failure_restores_replaced_directory(
    installed: dict[str, str], tmp_path: Path
) -> None:
    source = tmp_path / "refreshed-source"
    shutil.copytree(CANDIDATE_SOURCE, source)
    (source / "refresh.marker").write_text("same-version source refresh\n")
    before = snapshot(installed)
    control(installed, reload_fail_once=True)
    result = run_installer(installed, "install", check=False, source=source)
    assert result.returncode != 0
    assert snapshot(installed) == before
    assert (
        subprocess.check_output([installed["VAULTKEEP_BIN_LINK"], "--version"], text=True).strip()
        == CANDIDATE_VERSION
    )


def test_failure_after_move_restores_core_and_removes_trigger(
    installed: dict[str, str], tmp_path: Path
) -> None:
    source = tmp_path / "candidate-source"
    shutil.copytree(CANDIDATE_SOURCE, source)
    helper = source / "scripts/install_support.py"
    helper.write_text(
        helper.read_text().replace(
            'if command == "environment":',
            'if command == "environment" and "/releases/" in args[0]:\n'
            '        fail("injected post-move check failure")\n'
            '    if command == "environment":',
        )
    )
    before = snapshot(installed)
    result = run_installer(installed, "install", check=False, source=source)
    assert result.returncode != 0
    assert "injected post-move" in result.stderr
    assert snapshot(installed) == before


def test_manifest_replace_failure_cleans_temporary_file_and_restores_deployment(
    installed: dict[str, str], tmp_path: Path
) -> None:
    source = tmp_path / "candidate-source"
    shutil.copytree(CANDIDATE_SOURCE, source)
    helper = source / "scripts/install_support.py"
    content = helper.read_text()
    replacement = "os.replace(temporary, layout.manifest)"
    assert content.count(replacement) == 1
    helper.write_text(
        content.replace(replacement, 'raise OSError("injected manifest replacement failure")')
    )
    before = snapshot(installed)

    result = run_installer(installed, "install", check=False, source=source)

    assert result.returncode != 0
    assert "injected manifest replacement failure" in result.stderr
    manifest_path = Path(installed["VAULTKEEP_INSTALL_PREFIX"]) / "install-manifest.json"
    assert not manifest_path.with_suffix(".tmp").exists()
    assert snapshot(installed) == before


def test_later_version_retains_previous(host: dict[str, str], tmp_path: Path) -> None:
    wheelhouse_available(host)
    run_installer(host, "install")
    before = manifest(host)
    source = tmp_path / "later-source"
    shutil.copytree(CANDIDATE_SOURCE, source)
    for file in ("pyproject.toml",):
        path = source / file
        path.write_text(path.read_text().replace(f'"{CANDIDATE_VERSION}"', '"0.3.0"'))
    run_installer(host, "update", source=source)
    data = manifest(host)
    assert data["version"] == "0.3.0"
    assert "components" not in data
    assert data["retained_release"] == before["active_release"]
    assert Path(before["active_release"]).is_dir()


def test_paused_enabled_timer_keeps_enablement_and_timestamp(installed: dict[str, str]) -> None:
    system = Path(installed["VAULTKEEP_SYSTEMD_ROOT"])
    wants = system / "timers.target.wants"
    wants.mkdir()
    enabled = wants / "vaultkeep@app.timer"
    enabled.symlink_to(system / "vaultkeep@.timer")
    stamp = Path(installed["HOST_LOG"]).parent / "timer-stamp"
    stamp.write_bytes(b"persistent timer timestamp\n")
    before = (os.readlink(enabled), stamp.read_bytes(), stamp.stat().st_mtime_ns)
    control(
        installed,
        units="vaultkeep@app.timer loaded inactive dead timer\n",
        properties="LoadState=loaded\nActiveState=inactive\nSubState=dead\n"
        f"FragmentPath={system}/vaultkeep@.timer\nDropInPaths=\nExecStart=\nUnitFileState=enabled\n",
    )
    run_installer(installed, "install")
    assert (os.readlink(enabled), stamp.read_bytes(), stamp.stat().st_mtime_ns) == before
    result = run_installer(installed, "uninstall", check=False)
    assert result.returncode != 0
    assert "disable" in result.stderr


def component_fixture(host: dict[str, str], combined: bool) -> dict:
    """Reproduce the schema-2 deployment that predates the unified package."""
    data = legacy_fixture(host, "0.2.0")
    prefix = Path(host["VAULTKEEP_INSTALL_PREFIX"])
    old_release = Path(data["active_release"])
    release = old_release.with_name(old_release.name + ("-with-trigger" if combined else ""))
    if combined:
        old_release.rename(release)
        current = prefix / "current"
        current.unlink()
        current.symlink_to("releases/" + release.name)
        for record in data["owned_artifacts"]:
            if record["path"] == str(old_release):
                record["path"] = str(release)
            if record["path"] == str(current):
                record["target"] = "releases/" + release.name
        binary = Path(host["VAULTKEEP_TRIGGER_BIN_LINK"])
        binary.symlink_to(current / "venv/bin/vaultkeep-trigger")
        data["owned_artifacts"].append(
            {"path": str(binary), "type": "symlink", "target": os.readlink(binary)}
        )
        for name, command in (
            ("vaultkeep-trigger.service", "monitor"),
            ("vaultkeep-trigger-exec@.service", "exec-worker %i"),
        ):
            unit = Path(host["VAULTKEEP_USER_SYSTEMD_ROOT"]) / name
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(f"[Service]\nExecStart=/usr/local/bin/vaultkeep-trigger {command}\n")
            unit.chmod(0o644)
            data["owned_artifacts"].append(
                {
                    "path": str(unit),
                    "type": "file",
                    "mode": "0o644",
                    "sha256": hashlib.sha256(unit.read_bytes()).hexdigest(),
                }
            )
    data.update(
        schema_version=2,
        components=["core", "trigger"] if combined else ["core"],
        active_release=str(release),
    )
    (prefix / "install-manifest.json").write_text(json.dumps(data))
    (release / "deployment.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "version": "0.2.0",
                "source_digest": data["source_digest"],
                "components": data["components"],
            }
        )
    )
    return data


@pytest.mark.parametrize("combined", [False, True])
def test_component_deployment_upgrades_to_unified_package(
    host: dict[str, str], combined: bool
) -> None:
    old = component_fixture(host, combined)
    run_installer(host, "update")
    current = manifest(host)
    assert current["schema_version"] == 3 and "components" not in current
    assert current["retained_release"] == old["active_release"]
    assert Path(old["active_release"]).is_dir()
    assert not Path(host["VAULTKEEP_TRIGGER_BIN_LINK"]).exists()
    assert not (Path(host["VAULTKEEP_USER_SYSTEMD_ROOT"]) / "vaultkeep-trigger.service").exists()


def test_failed_legacy_combined_upgrade_restores_old_links_and_units(host: dict[str, str]) -> None:
    component_fixture(host, True)
    before = snapshot(host)
    control(host, reload_fail_once=True)
    result = run_installer(host, "update", check=False)
    assert result.returncode != 0
    after = snapshot(host)
    ignored = ("install.lock", ".example")
    assert {key: value for key, value in after.items() if not key.endswith(ignored)} == {
        key: value for key, value in before.items() if not key.endswith(ignored)
    }
