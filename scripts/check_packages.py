"""Build and verify the unified Vaultkeep wheel and self-contained source archive.

All builds/installations use temporary paths and no services or production data.
"""

from __future__ import annotations

import configparser
import email
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*command: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(command, cwd=cwd, env=env, text=True, stderr=subprocess.STDOUT)


def build(project: Path, output: Path, kind: str = "") -> None:
    arguments = [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(output)]
    if kind:
        arguments.append("--" + kind)
    run(*arguments, str(project))


def check_wheel(wheel: Path, version: str) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert {n.split("/", 1)[0] for n in names} <= {
            "vaultkeep",
            wheel.name.removesuffix("-py3-none-any.whl") + ".dist-info",
        }
        assert "vaultkeep/trigger/monitor.py" in names
        assert "vaultkeep/py.typed" in names
        assert "vaultkeep/notifications/notifier.py" in names
        assert "vaultkeep/notifications/desktop.py" in names
        assert "vaultkeep/backup/notifications.py" in names
        assert "vaultkeep/trigger/notify.py" not in names
        assert "vaultkeep/trigger/desktop.py" not in names
        assert "vaultkeep/maintenance.py" in names
        metadata_path = next(n for n in names if n.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_path))
        assert metadata["Version"] == version
        assert metadata["Requires-Python"] == ">=3.13"
        assert any(d.startswith("psutil") for d in metadata.get_all("Requires-Dist", []))
        assert not any(
            d.startswith("vaultkeep-trigger") for d in metadata.get_all("Requires-Dist", [])
        )
        entry_path = next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
        entries = configparser.ConfigParser()
        entries.read_string(archive.read(entry_path).decode())
        assert set(entries["console_scripts"]) == {"vaultkeep"}
        assert any("LICENSE" in n for n in names)


def check() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    environment = dict(os.environ, PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    environment.setdefault("PIP_FIND_LINKS", "/tmp/vaultkeep-wheelhouse")
    with tempfile.TemporaryDirectory(prefix="vaultkeep-packages-") as directory:
        base = Path(directory)
        output = base / "dist"
        build(ROOT, output)
        wheel = next(output.glob("*.whl"))
        check_wheel(wheel, version)
        with tarfile.open(next(output.glob("*.tar.gz"))) as archive:
            members = archive.getnames()
            assert not any(
                part in n.split("/")
                for n in members
                for part in (
                    ".git",
                    ".venv",
                    "__pycache__",
                    ".aws",
                    ".env",
                    ".codex",
                    ".agents",
                    "components",
                )
            )
            archive.extractall(output / "extracted", filter="data")
        extracted = next((output / "extracted").iterdir())
        for required in (
            "src/vaultkeep/py.typed",
            "src/vaultkeep/trigger/cli.py",
            "src/vaultkeep/notifications/notifier.py",
            "src/vaultkeep/notifications/desktop.py",
            "src/vaultkeep/backup/notifications.py",
            "docs/notifications.md",
            "requirements.lock",
            "scripts/install_support.py",
            "scripts/install_maintenance.py",
            "src/vaultkeep/maintenance.py",
            "systemd/vaultkeep-trigger-exec@.service",
            "install.sh",
            "examples/vaultkeep-schedule.yaml",
            "examples/electrum-trigger.yaml",
            "examples/bisq-trigger.yaml",
            "examples/wasabi-trigger.yaml",
        ):
            assert (extracted / required).is_file(), required
        build(extracted, output / "rebuilt", "wheel")
        os_release = base / "os-release"
        os_release.write_text("ID=debian\nVERSION_ID=13\nVERSION_CODENAME=trixie\n")
        preview = base / "preview"
        env = dict(
            environment,
            VAULTKEEP_INSTALL_TESTING="1",
            VAULTKEEP_TEST_OS_RELEASE=str(os_release),
            VAULTKEEP_INSTALL_PREFIX=str(preview / "opt/vaultkeep"),
            VAULTKEEP_CONFIG_ROOT=str(preview / "etc/vaultkeep"),
            VAULTKEEP_VAR_ROOT=str(preview / "var/vaultkeep"),
            VAULTKEEP_BIN_LINK=str(preview / "bin/vaultkeep"),
            VAULTKEEP_TRIGGER_BIN_LINK=str(preview / "bin/vaultkeep-trigger"),
            VAULTKEEP_SYSTEMD_ROOT=str(preview / "systemd/system"),
            VAULTKEEP_USER_SYSTEMD_ROOT=str(preview / "systemd/user"),
        )
        run("bash", str(extracted / "install.sh"), "install", "--dry-run", env=env, cwd=extracted)
        assert not preview.exists()
        venv = base / "env"
        run(sys.executable, "-m", "venv", str(venv))
        python = str(venv / "bin/python")
        run(
            python,
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(ROOT / "requirements.lock"),
            env=environment,
        )
        run(python, "-m", "pip", "install", "--no-deps", str(wheel), env=environment)
        run(
            python,
            "-c",
            "import sys, vaultkeep.notifications.notifier; "
            "assert 'vaultkeep.trigger' not in sys.modules; "
            "import vaultkeep.trigger.monitor; import importlib.util; "
            "assert importlib.util.find_spec('vaultkeep_trigger') is None",
        )
        command = str(venv / "bin/vaultkeep")
        assert run(command, "--version").strip() == version
        for example in (ROOT / "examples").glob("*.yaml"):
            run(command, "--config", str(example), "validate", "--schema-only")
            run(command, "--user", "--config", str(example), "validate", "--schema-only")
        assert not (venv / "bin/vaultkeep-trigger").exists()
        run(python, "-m", "pip", "check")
        print("Unified wheel, source archive, installer preview and all job examples passed.")


if __name__ == "__main__":
    try:
        check()
    except subprocess.CalledProcessError as error:
        print(error.output, file=sys.stderr)
        raise
